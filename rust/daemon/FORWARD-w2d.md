# W2-D forward review checklist

The paused search branch predates the shared writer seam and complete startup
hydration. This branch merges master and preserves the existing W2-D search,
cortex block, chronicle, timeline, contiguity, reranker and telemetry work.
It is a local handoff for the paused author; it adds no remaining read routes.

## History and scope

- Original branch: `rust-cf/w2d-search`, `65e3178ae85cb13f7c40da26e5b401b4ca496db9`.
- Forward branch: `rust-cf/w2d-search-fwd`.
- First merged master: `4b34c81ef053ae2966ac8605d2680c36a182e6cb`.
- First merge/code head: `3f0ce1ea4fdb309a6e5c6367e32cd7e4914e7171`, with that
  master and the original branch as parents.
- Harness follow-up: `5e47a4fbb26ad6dbbc2580379e5586862b4fd595` adds one
  chronicle-limit case and its coverage assertion; no production code change.
- Final merged master: `4a8a11a799679b711fc647ce1e0a383b95051c8b` (HTTP admission
  and schema-review rules advanced during validation).
- Final code/harness head: `7958d049e3bd6f10a5e14a2ade7406f412ebb0b3`.
- Original branch and its worktree were not changed. No public push or PR.
  Transfer to the assigned Linux lane used a Git bundle and a separate checkout.
- `GET /api/search` is the only route dispatched by `read::get`; the rest of
  `PHASE3-READ-PATH.md`, recall/world/lesson routes and the reference-store blocker
  remain the paused author's work. Existing limitations remain in `divergences.md`.

## Conflicts (22 distinct files)

Paths below are relative to `rust/`.

| File | Resolution and reason |
|---|---|
| `.gitattributes` | Master: current harness artifact attributes. |
| `Cargo.lock` | Master lock, then Cargo resolves the combined manifests; no hand merge of lock entries. |
| `daemon/Cargo.toml` | Master: both existing slice dependencies and the graph-harness binary/feature remain. |
| `daemon/divergences.md` | Master ledger plus the W2-D search section from the paused branch. |
| `daemon/harness/daemons.py` | Master: shared environment isolation, static/loopback fixtures and port override handling. |
| `daemon/harness/gen_schema_sql.py` | Master: current shared schema fixture generation. |
| `daemon/harness/pgdisposable.py` | Master: current disposable database admission and cleanup. |
| `daemon/harness/record_routes.py` | Master: current shared route recording. |
| `daemon/harness/run.py` | Master: current shared scenarios, declared changes and comparisons; W2-D-specific scripts remain separate. |
| `daemon/src/bank.rs` | Master seating, dynamic matrix dimension, original embeddings and startup fields; retain W2-D atomic resident access counts, including dump and retention-score reads. |
| `daemon/src/config.rs` | Master parser, dump, startup bands and embedding fields; add W2-D reranker/cortex/traces settings and stale-policy/cortex-BM25 fields, defaults and readers. |
| `daemon/src/embed.rs` | Master: complete artifact resolution, configured pooling, validation and embedder LRU. |
| `daemon/src/health.rs` | Master: shared health behavior and tests. |
| `daemon/src/http.rs` | Master gates, Python JSON writer, static handling and coordination-start; replace only master's search dispatch with W2-D dispatch and remove the superseded search implementation. |
| `daemon/src/main.rs` | Master startup/probe/module wiring plus W2-D `read` and `stores`. |
| `daemon/src/mutants.rs` | Master fixture-capability helpers; W2-D mutants use the same generic name selector and need no additional registry. |
| `daemon/src/search.rs` | W2-D ranking and tests; adapt cosine indexing/fixtures to master's `Bank.dim` and startup entry fields; retain master's Python `str` conversion for JSON slot values. |
| `daemon/src/service.rs` | Master all-or-nothing startup, detached init, clock-only reseed retry and embedder loading; add resident W2-D cortex to the candidate before publication, and retain W2-D warmup search after successful init. |
| `daemon/src/static_files.rs` | Master: shared static route contract. |
| `daemon/src/storage/mod.rs` | Master: leased writer session and current storage API. |
| `daemon/src/storage/schema.rs` | Master: current schema startup/validation. |
| `daemon/src/storage/tests.rs` | Master: current storage fixtures and tests. |

`txn.rs`, startup modules, CI shard layout and producer census came from master
without a conflict and were not redesigned.

The final master merge had one repeated conflict, in the imports of `http.rs`:
retain master's `HttpBody` import and W2-D's removal of the obsolete `search`
import. All new HTTP admission/drain code, dependency additions, CI and harness
changes came from master unchanged; W2-D dispatch remains at its existing seam.
The latest ruleset also came from master, with no authored ruleset edits.

## Writer seam adoption

Every production W2-D DB access uses the leased `Ready.storage.client()`.
No product call site owns a new dedicated connection.

| Entry point | Guarded operations |
|---|---|
| `read/search_route.rs::run` | `txn::with_client` around chronicle lookup and retrieval-event logging. `chronicle::events_for`, `identity::session_id`, `identity::open_leaf_episode` and private `log_event` use that borrowed client directly; no nested guard. |
| `read/cortex_search.rs::cortex_search` | `txn::with_client` around the complete cortex result: traces, trace invalidations and the single-statement slot-read upsert. Private `_locked` helper owns the borrowed-client operations. |
| `read/cortex_search.rs::attach_served_facts` | `txn::with_client` around retrieval-event served-fact telemetry; the private helper preserves the original best-effort write behavior. |
| `stores/cortex.rs::hydrate` | `txn::with_client` around the facts load and resident store construction. |
| `service.rs` / `bank.rs` | Master's guarded startup candidate, metadata, seating and clock reseed remain intact. |

The old slice-only `db_guard` is renamed `search_guard`: it preserves ordering
of resident access accrual and response telemetry across CPU work and the cortex
block. It is not the DB transaction guard and does not share or replace `OPEN`.
No W2-D multi-statement transaction remains to convert to `txn::run`; its two
telemetry writes were already single autocommit statements.

## Side branches

| Branch/head | Already represented in paused search? | Forwarding decision |
|---|---|---|
| `w2d-cortex` / `89335368d5b69ff2f59ea648a1ae25ba7a55e923` | Yes. Its three files are present. Later search integration adds config wiring, replaces the slot-read loop transaction with one atomic upsert, and improves pinned-unranked coverage; the resident cortex implementation remains. | Nothing missing to apply on a scratch branch; do not merge or forward the old side branch separately. |
| `w2d-rerank` / `19ad17ac2466a8346fe90d9ab88bdd9d25ca9ba5` | Yes. The parity script is byte-identical; `rerank.rs` differs only by removal of its temporary dead-code allowance and wiring comment. | Nothing missing to apply on a scratch branch; do not merge or forward it separately. |

Verified with `git diff <side-head> 65e3178a -- <slice-owned files>` and
`git diff f617f257 <side-head> --stat`, rather than relying on ancestry:
the main branch incorporated the contents without those side commits as parents.

## Decisions for the paused author

- Shared startup wins: W2-D's resident cortex is loaded after master's complete
  candidate and before publishing `Ready`. It currently reloads facts instead of
  constructing its typed store from `StartupState.cortex`; converting that store
  representation is deliberately deferred.
- The resident cortex loader still assumes 1024 dimensions, as the paused slice
  did. Master's entry matrix dimension is preserved. Generalizing the cortex
  loader for other configured embeddings is an author decision.
- Both master's per-embedder LRU and W2-D's outer query LRU remain. Removing the
  outer cache or changing its ownership/key is a design decision, not a forward fix.
- The master configuration dump keeps its existing field inventory. W2-D-specific
  fields are parsed and consumed, but extending shared config goldens is deferred.
- There are no changes under `pseudolife_memory/` between the paused head and
  merged master (`git diff --name-only 65e3178a 4b34c81e -- pseudolife_memory` is
  empty). No Python-oracle golden re-recording was performed.
- The assigned lane's old W2-D venv contains sentence-transformers 5.7.0.
  Master `harness/embedding.py` names 5.3.0 / transformers 4.57.6 as its measured
  contract. The first live run with 5.7 failed on raw padded-query cortex scores:
  Python 0.6645/0.5937/0.5656 versus Rust 0.6429/0.5637/0.5397, and the same
  served-fact scores in telemetry. Master tokenization strips boundary whitespace;
  the former slice embedder did not. An isolated 5.3 overlay was prepared without
  changing the old venv or shared source. Supporting both library semantics is a
  paused-author decision; this forward retains master embedding behavior.
- Search goldens under `harness/goldens/w2d/` are absent in the paused branch and
  the forwarded branch. Golden replay cannot run without recording them; that
  decision remains with the paused author.
- Independent publication review and any public PR belong to the resumed author
  and delegate. This forward task is local-only.

## Validation

The first set of results below is for the code tree at `3f0ce1ea`. The harness
follow-up and final master merge are called out separately. Final checklist-only
commits change neither production nor harness code. Evidence logs are private
and named by W2D.

| Environment / command | Result |
|---|---|
| Windows `cargo fmt --all --check --manifest-path rust/Cargo.toml` | Exit 0 (`W2D-fmt.log`). |
| Windows `cargo clippy -j 2 --all-targets -p pseudolife-daemon --manifest-path rust/Cargo.toml` | Exit 0, no warnings (`W2D-clippy.log`). |
| Windows `cargo test -j 2 -p pseudolife-daemon --manifest-path rust/Cargo.toml` | Exit 0; 119 passed, 2 ignored (`W2D-unit.log`). DB tests without their opt-in DSN return early; this is not a claim that those DB fixtures ran. |
| Windows disposable PG: `served_facts_recovers_abandoned_writer_transaction` | Watched RED: exit 101, pending marker 1 (`W2D-seam-red.log`); after seam adoption GREEN: exit 0, pending marker 0 (`W2D-seam-green.log`), repeated on committed code head, exit 0 (`W2D-seam-final.log`). Created/dropped only `pl_cf_w2d_guard_*` databases. |
| Windows `python -m pytest rust/ci/test_workflow.py -q` | Exit 0; 33 passed (`W2D-ci-layout.log`). |
| Assigned Linux lane `cargo build -j 3 -p pseudolife-daemon --features mutants --manifest-path rust/Cargo.toml` | Exit 0 (`build.log`, `build.exit`). |
| Linux disposable end-to-end cell: `w2d_search.py live --only log-off-control` | Exit 0; 3 cases, zero response/state diffs (`cell.json`). |
| Linux `w2d_search.py live --reuse-template`, sentence-transformers 5.3.0 / transformers 4.57.6 / torch 2.14.1+cpu | Exit 0; 11 scenarios, 118 cases, zero response diffs, zero DB-state diff scenarios (`live-st53-v2.json`). Same code and comparison rules as the first run. |
| Linux updated default at harness head `5e47a4fb`: `w2d_search.py live --only default --reuse-template` | Exit 0; 59 cases, zero response/state diffs; `chronicle_over_limit=1` (`default-limit-fix.json`). |
| Linux focused `search-chronicle-limit` control with the updated default | Exit 0 for the control runner; 1 differing case, no survivor (`chronicle-control-fixed.log`). Together with the earlier ten caught controls, all eleven have detection evidence. This is a focused follow-up, not a second full mutant run. |
| Publication check, `origin/master..rust-cf/w2d-search-fwd` | Exit 0 at merge/code head (`W2D-publication.log`); repeated after the checklist commit. |

The first live run, using the existing 5.7 venv, exited 1: 11 scenarios, 118
cases, 2 differing responses, 1 differing DB-state scenario (`live.json`). The
padding-score mismatch above accounts for the state difference. The second
response difference was a wall-clock boundary (`just now` versus `1 minute
ago`); no comparison tolerance was changed. Controls started on that failing
baseline were abandoned and are not counted as validation.

The first isolated 5.3 launch failed before any case with a missing checkout
import; the private launcher now sets `PYTHONPATH` to the forwarded checkout.
Its setup failure logs are retained separately from the matching-environment run.

The matching-environment full mutant run exited 1 (`mutants-st53-v2.log`):
10 controls caught their intended breaks, but `search-chronicle-limit` survived
with zero diffs. This is a pre-existing coverage gap: the old default queries
match at most three seeded events, so a limit of three is indistinguishable.
The invalid 5.7 baseline had masked that gap with unrelated diffs.

The harness-only follow-up adds one aggregation query whose release/bank/daemon
terms match all five seeded events, and requires `chronicle_over_limit >= 1` in
the default scenario. It changes no seed data, goldens, tolerance or daemon code.
Only the touched default scenario and that surviving control are rerun; the ten
other scenarios and ten caught controls keep their earlier unchanged-code evidence.

The first private cleanup inventory used the disposable-only DSN helper for
`postgres`, which correctly refused before connecting (exit 1, `cleanup.log`).
The inventory uses the harness's existing administrative connection helper on
the isolated test server instead; database drops still use its checked `drop`.
Final cleanup exited 0 (`cleanup-fixed.log`): the owned seed template was
dropped, no owned scenario databases remain, and no active owned workload or
daemon processes remain. Private model fixtures, source checkout, venv, logs and
Rust cache are retained for review. No full suites, GPU launches or deploys ran.

## Final master overlap validation

The final merge at `7958d049` imports master's HTTP admission change and required
dependencies. W2-D ranking, stores, writer seams and the strengthened harness are
unchanged. Validation selects the affected HTTP/CI checks plus the updated
default search scenario and chronicle control; the ten other search scenarios
and ten caught controls retain the earlier evidence for their unchanged modules.

| Check at `7958d049` | Result |
|---|---|
| Windows `cargo fmt --all --check` | Exit 0 (`W2D-fmt-final.log`). |
| Windows `cargo clippy -j 2 --all-targets -p pseudolife-daemon` | Exit 0, no warnings (`W2D-clippy-final.log`). |
| Windows `cargo test -j 2 -p pseudolife-daemon` | Exit 0; 124 passed, 2 ignored (`W2D-unit-final.log`); opt-in DB-fixture limitation above still applies. |
| Windows `python -m pytest rust/ci/test_workflow.py rust/daemon/harness/test_body_cases.py -q` | Exit 0; 56 passed (`W2D-http-overlap.log`). |
| Windows disposable PG writer-recovery case | Exit 0 on committed `7958d049`, pending marker absent (`W2D-seam-latest.log`). |
| Linux `cargo build -j 3 -p pseudolife-daemon --features mutants` | Exit 0, no warnings (`http-build.log`). |
| Linux updated default against a newly seeded disposable template | Exit 0; 59 cases, zero response/state diffs, `chronicle_over_limit=1` (`http-read-default.json`). |
| Linux focused chronicle-limit control | Exit 0 for the runner; 1 differing case, no survivor (`http-chronicle-control.log`). |

Latest-head cleanup exited 0 (`http-cleanup.log`): the newly seeded owned
template was dropped, no owned scenario databases remain, and no active owned
workload or daemon processes remain. Final publication validation is exit 0 over
`origin/master..rust-cf/w2d-search-fwd`; the handoff contains no public push or PR.
