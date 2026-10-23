# Rust cutover plan and checklist

The Rust daemon and CLI already serve most of the memory read, write and
board paths. They run beside the Python daemon, on disposable test banks
only. This page lists what is left before Rust can become the default and
Python daemon development can stop. The work is in an order that lets
independent batches run at the same time, and every decision that is the
maintainer's to make is named. Nothing here authorizes a default switch, a
release, an update, a deploy or any live-bank change.

Plan audit: master `98c5d6bf`, 2026-10-19, updated for #832 (`2ce04a7d`);
revised 2026-10-20 over master `2092e7b1` with what landed since, what sets
the pace, and three proposals that wait on the maintainer (proposals A to C, below).
The surface counts come from the refreshed checklists in
[PARITY.md](PARITY.md). The acceptance checklists after the plan are the
2026-10-10 draft's per-item evidence rules, unchanged.

## Decisions this plan rests on

| Date | Decision | Effect on the plan |
|---|---|---|
| 2026-10-19 | Maintainer: "Let's do graph and dream after all". | Reverses the 2026-10-12 deferral recorded in PARITY.md ("Graph traversal deferred by decision"). Graph expansion in recall, `memory_graph` traversal, the deep-dream edge pass and the graph-insight refresh return to scope with the rest of the W3-H graph and dream engine, as tasks M to P (batch B1). Task N2's PR (`memory_recall`'s graph expansion, after #847) updates PARITY.md's decision section, the READ-CORE and GRAPH-REVIEW rows and the matching `daemon/divergences.md` rows together with the traversal code, as agreed with the Rust port session on 2026-10-19. Until then those lines still read "deferred by decision". |
| 2026-10-19 | Maintainer, relayed by the delegate: graph insight takes **Option B** for task P2b. The Rust daemon computes communities, and the sampled betweenness behind god nodes and surprises, with a deterministic Rust algorithm (no RNG, stable tie-breaking) instead of porting NetworkX's Louvain and Python's Mersenne Twister. | P2b's PR records it in `daemon/divergences.md` as an accepted divergence and compares it structurally, not byte for byte: community count and size distribution, a partition-agreement measure, and the god-node and surprise sets, with the agreement threshold stated in the PR. No NetworkX pin. Betweenness sampling gets the same treatment unless exact betweenness is cheap at bank sizes; the PR states the choice and why. |
| 2026-10-19 | Maintainer: PRs land in integration batches built by `ops/integrate.py` (#851), because GitHub's merge queue is not available on a user-owned repository. | One CI run per batch instead of one per PR per master move. This is the pace model under "What sets the pace" below. |
| 2026-10-12 | Maintainer: graph expansion in recall and the deep-dream edge pass are not ported (reaffirmed 2026-10-16). | Superseded by the 2026-10-19 decision. The eval evidence behind it stays recorded in PARITY.md. |
| 2026-10-09 | Maintainer: documents move from the Chroma reference store to a pgvector table with exact cosine search. | The two document tools and the reference pool in search wait for that Python table (batch B7). Schema v56 shipped judge votes (#807), not the table. |
| 2026-10-09 | Delegate ruling: an MCP tool whose body has not landed answers the declared `not_implemented` stub, as unserved routes answer their declared 501. | Every unserved surface below is a declared stub or 501, never a silent success. |

## Where the port stands

| Surface | On master `2092e7b1` (2026-10-20) | Left to do |
|---|---|---|
| MCP tools (38) | 31 served (`mcp_compare.py`'s `UNSERVED` lists the other 7). Since the first audit: `memory_consolidation_candidates` (#833, batch #850) and the four graph write tools `memory_graph_relate`, `memory_graph_unrelate`, `memory_alias` and `memory_relation_define` (#842, task M, batch #866). In flight: #836 `memory_consolidate` (task L), #847 `memory_graph` (task N1, stacked on #842), #864 and #867 `memory_dream` (task P1: the consolidation pass, then outcome inference, lesson synthesis and digests). | 7: `memory_consolidate`, `memory_graph`, `memory_recall` (graph expansion, task N2), `memory_graph_review` (task O), `memory_dream` (after task P1, the graph-insight refresh P2b under Option B), and `document_ingest` and `document_search`, blocked on the Python document table. `memory_search` still refuses `explain=true`. |
| Console routes (72 `ConsoleRoutes`, plus `POST /api/pair`) | 2 served (`GET /api/search`, `POST /api/daemon-notice`), 1 partly (`GET /api/agents`: the coordination view). `/api/pair` serves its refusals; redemption is 501. | 69 routes answer 501: 32 GET and 37 POST ([Console checklist](PARITY.md#complete-console-registration-checklist)). Proposal A below would retire 8 of them and move 3 more to the CLI batches. |
| Hook endpoints (8) | 4 served (coordination-start, park-gate, woke, subagent). memory-changes and session-end serve their gates and refusals. | 4: session-start and memory-policy (501 for every caller), the authorized memory-changes body and the session-end close. |
| Coordination actions (15) | All served and compared by the board harness. | Three declared cutover blockers (below). |
| CLI modes (29) | 9 native, 15 partly native, 5 defer by name ([CLI checklist](PARITY.md#cli-mode-acceptance-checklist)). | The deferred sub-rows and modes in batch B10, plus the lite-tier rows (D1). |
| Background duties | The scheduler, session reaper, dream-journal and log pruning, heap trim, release check and stored-principal refresh are implemented. | Autosave and the clean-exit flush are scheduled no-ops. The compaction policy is ported and golden-tested but never invoked. The reaper's automatic titles are absent. Automatic dreams, graph sweeps and the session-end dream trigger are absent or a declared no-op. |
| Evidence | Every `ported` body rests on recorded Linux dev-lane differential runs (`w2d_search.py`, `w2e.py`, `board/run.py`). | Hosted CI compares no served MCP or REST body other than `memory_toolset`'s. It runs the MCP wire, binding and stub corpus, HTTP admission, security and body limits, static files, sweep pruning, dream cursors, the graph store and graph read model, and the CLI harness rows. No Windows runtime run of the daemon exists (batch B13). Since 2026-10-19 a local full suite runs in 9 to 12 minutes on four workers (#846, batch #850), and the CI reshape in batch #868 (merged 2026-10-20 as 72fbcc88) takes the Windows `cli-live` parity suite out of the required gate (still required on Linux; the other live suites stay gated on Windows) and runs only the parity shards a PR's diff reaches (#859), narrows the required Windows Python lane to the cases that reach Windows-specific product code (#862), and replaces sub-second clock waits in tests with event waits, with a guard against new ones (#863). Proposal C below would move the body differentials to a nightly dev-box run. |
| Declared divergences | About 150 rows in `daemon/divergences.md`, 9 in `daemon/spec.md` (W2-F) and a few dozen across the CLI harness rows and specs. | Each closed, or accepted by the maintainer (batch B14). |

### Named cutover blockers

| # | Blocker | Where declared | Closes in |
|---|---|---|---|
| 1 | Tokenless MCP Host/Origin rebinding: tokenless `/mcp` stays unmounted (501) until the rebinding gate is accepted | `daemon/divergences.md`, "Routes whose handlers belong to later slices" | B6, maintainer acceptance (D3) |
| 2 | Reference store: no document table, so no reference hits in search and no document tools | `daemon/divergences.md`, "W2-D: GET /api/search" | B7 |
| 3 | W2F-PAIRING-HTTP: a valid `/api/pair` body on an authenticated install answers 501 | `daemon/divergences.md`, "Coordination board completion (W2-F)" | B5 |
| 4 | W2F-CLOCK-RETRY: a send retry after an epoch change cannot heal the writer session | same | B5, after B4's writer-session refresh |
| 5 | W2F-SHARED-HLC: the board keeps its own clock instead of the service clock | same | B5 |
| 6 | Qwen artifact provisioning: no verified default embedding and reranker export is provisioned | `daemon/divergences.md`, "Startup and configuration" and `w2d-model-provisioning` | B8 (D4) |
| 7 | Image ONNX graph agreement: the image's graph is not the verified export | OPS-CUTOVER below | B8 (D4) |

Blockers 6 and 7 are one problem and go in one batch.

## Maintainer decisions and gates

Decisions (D) change what gets built; gates (G) are steps only the maintainer
takes. Each batch below names the ones it waits on.

| ID | Decision or gate | Why it is the maintainer's | Blocks |
|---|---|---|---|
| D1 | **Lite and embedded storage tiers: port or retire.** The lite tier (`pip install "pseudolife-mcp[lite]"`, an embedded PostgreSQL 18 through `pg0-embedded`) is the README Quickstart's install. `embedded` mode and file mode are the v0.1 in-process server and `.pt` file storage, an escape hatch with no Postgres. The Rust daemon refuses to start without a DSN. Options: port lite and retire embedded and file mode (keeping a one-time `.pt` import); port all three; or retire all three and make Docker the only tier. Session recommendation, not a decision: the first. the 2026-08-14 lite-backbone evaluation found that `pg0-embedded` is a thin wrapper over the Rust `postgresql-embedded` crate (re-verify upstream before relying on it), so lite's PostgreSQL is likely reachable natively, and the file-mode engine (torch tensors and Chroma) has no current install path. Rule 6 of PARITY.md's "Internal-only gaps requiring explicit equivalents" says no retirement of embedded mode or `.pt` is implied without this decision. | It changes the supported install tiers and retires user-visible modes. | B9; the lite-tier CLI sub-rows in B10 |
| D2 | Where counters, surprise history and band weights persist: Python's torch `weights.pt`, a port of that format, or a bank table (a schema change). | It is a durable format choice; a table is a schema bump. | B4 autosave |
| D3 | Accept the tokenless MCP rebinding gate (blocker 1). | The divergence register says the gate must be accepted, not just implemented. | B6 |
| D4 | Model provisioning: bake the verified Qwen export into the image or provision it at install; scope of bf16 ONNX. | It changes image contents and install downloads. | B8 |
| D5 | How existing Chroma document stores reach the new table: migrate at upgrade or re-ingest. | It decides what happens to users' ingested documents. | B7 |
| D6 | Evidence bar for `ported`: is a recorded Linux dev-lane differential run enough, or must hosted CI compare bodies on both OSes? This refresh applies the existing rule (a recorded run). | It decides how much of B13 is required before the switch. | B13, G2 |
| D7 | Accept or close each remaining declared divergence, including the two sent-YAML presentation deferrals below. | CUTOVER requires the union to be empty or accepted by the maintainer. | B14, G2 |
| D8 | Port or retire `delegate-watch` and the remaining operator-only CLI modes (`coordination-recovery`, `channel`'s remainder, `maintainer setup`). | Retiring a mode removes a supported command. | B10e |
| D9 | **Proposal A: Console scope cut.** Retire, or serve only to their CLI callers, the 12 unserved Console routes that no Console code calls (listed under "Proposals awaiting the maintainer"). | It removes or narrows public REST routes that the guides document. | The sizes of B2a, B2b and B2c |
| D10 | **Proposal B: a soft Python freeze now.** Fixes only, no new Python surfaces, pending schema changes batched into one bump: G4's "fixes only" half brought forward, without the retirement half. | It stops feature work on the daemon users run today. | Every batch's oracle stays still |
| D11 | **Proposal C: nightly dev-box evidence.** Run `w2e.py`, `w2d_search.py` and `board/run.py` nightly on a Linux dev box and publish the JSON, instead of putting the body comparisons into hosted CI. | With D6 it sets the evidence bar for `ported` and for the switch. | B13's scope, G2 |
| G1 | Start the live-bank shadow and the soak (B16). | Live-bank access. | B16 |
| G2 | Switch the default: installers and release-mode `update` select the Rust daemon. | Default switch. | B17 |
| G3 | Release on the four public surfaces, then deploy. | Release and deploy. | B17 |
| G4 | Freeze Python daemon development (fixes only), and later retire the Python oracle, source and packaging. | Oracle retirement. | B17 |

## Proposals awaiting the maintainer (2026-10-20)

These three came out of the 2026-10-19 review of this plan. None is decided;
each is a row in the decisions table above (D9 to D11), and the batches
below say what changes if it is accepted.

**Proposal A (D9): cut the Console scope.** A static search of master `2092e7b1`
(`git grep` over `frontend/src`, `pseudolife_memory`, `plugin`, `rust/shim`,
`ops`, `evals` and `examples`, excluding tests and `web/routes.py` itself)
finds no Console caller for 12 of the 69 unserved routes. The 8 below have no
caller at all outside `routes.py`, their tests and the guides that document
them (`docs/guide/episodes.md`, `dreaming.md`, `memory-model.md`,
`configuration.md`, `docs/runbooks/deep-dream.md`). What retiring each would
leave: the two restores survive as `memory_graph_review(action=
"restore_slot")`; the community summary survives in `GET /api/graph/digest`
(the Insight view's source), but per-community detail
(`/api/graph/communities?id=`) does not; episode merge, prune and rename,
proposal evidence and the retired-curation listing have no other path
(`mcp_server.py` has no such actions, and `memory_session_title` renames only
the caller's own session), so retiring those five removes the capability,
not just the route. Each is a documented REST API removal and needs a
deprecation note.

- `GET /api/curation/retired`
- `GET /api/graph/communities`
- `GET /api/graph/proposal-evidence`
- `POST /api/episodes/merge`
- `POST /api/episodes/prune`
- `POST /api/episodes/rename`
- `POST /api/lessons/restore`
- `POST /api/world/restore`

The other 4 have CLI, shim and eval-harness callers (`evals/
coordination_bench.py`, `agent_token_ledger.py`, `memory_policy_bench.py`,
`taubench_pseudolife`), no Console caller. They stay served. `/api/briefing`
is already B3's; the other three belong to the CLI batches (B10) rather than
to the Console batches. They could move onto MCP if the maintainer prefers
one surface, which moves those callers too:

- `GET /api/briefing` (briefing CLI, session hook, shim briefing hook)
- `GET /api/stats` (`move` CLI, shim `sent` serve)
- `POST /api/episode/start` and `POST /api/episode/end` (episode CLI,
  `service.py`, `shim.py`, shim lifecycle)

If accepted, B2b loses the five episode routes and the two restores, B2a
loses `/api/stats`, and B2c loses `/api/curation/retired`,
`/api/graph/communities` and `/api/graph/proposal-evidence`: 11 routes leave
the Console batches, 1 to 2 PRs fewer, and 8 fewer Python route contracts
for the Rust side to match.

**Proposal B (D10): a soft Python freeze now.** Every new Python surface widens the
oracle the Rust side chases, and every schema bump moves the parity pin and
the harness goldens on both OSes (the v56 lesson in CLAUDE.md). The proposal
brings G4's "fixes only" half forward to now: fixes and the document table
(B7) continue; no new Python tools, routes, hooks or CLI modes; pending
schema changes (B7's table, D2 if it picks a table) land in one bump. The
retirement half of G4 stays at stage 4.

**Proposal C (D11): nightly evidence on a Linux dev box instead of hosted
body comparisons.** B13 as written puts `w2e.py`, `w2d_search.py` and
`board/run.py` into hosted CI on both OSes through portable goldens or a
provisioned oracle: 3 to 5 PRs, Windows runners that are already the slowest
lane, and real-model runs hosted CI cannot afford. The proposal runs the
three differentials nightly on a Linux dev box against master, publishes the
JSON (counts, differences, the compared head) where PRs can cite it, and
keeps hosted CI on what it does now. D6's bar is then "a published nightly
run on the current master" rather than a hosted job. B13 shrinks to the
stale tokenless `woke` case, Windows daemon runtime runs if D1 keeps lite,
and whatever hosted comparison D6 still requires.

## Ordered plan

Sizes are in PRs, from the scope each batch names; they carry real error
bars and have no dates. Each batch owns the files listed. Two batches whose
files do not overlap can run in parallel sessions.

Shared hot spots, and how batches share them:

- `rust/daemon/src/http.rs`. B2-0 is the plan's first PR: it moves the route
  fall-through (the dispatch tail that ends in `not_implemented`), the hook
  arm, `pair()` and the `/mcp` mount into per-family modules, with no
  behavior change, so B2a to B2d, B3, B5 and B6 each edit their own module.
  B3, B5 and B6 start after it rather than serializing on `http.rs`.
- `rust/daemon/src/mcp/dispatch.rs` (the tool registry) and
  `daemon/harness/mcp_compare.py` (`UNSERVED`): only B1 and B7 add tools, one
  PR at a time. B6 also edits `mcp_compare.py` (the tokenless cases) and
  takes its turn in that queue.
- `daemon/harness/w2e.py`: B1 and B2b both add scenarios; they serialize on
  that file or put new scenarios in separate modules it imports.
- `rust/daemon/src/service.rs`: B1's background ticks and B4 both edit it, so
  they serialize on that file.
- `.github/workflows/rust.yml` and `rust/ci/`: B8 (image CI), B10 (harness
  rows) and B13 (hosted parity) add jobs, one PR at a time.
- `pseudolife_memory/storage/schema.py` and the Rust schema plan: B7 only,
  plus D2 if it picks a table. Any schema bump serializes with every other.
- `rust/PARITY.md` and `rust/daemon/divergences.md`: each batch edits only
  its own rows. B14's register-wide pass runs when few batches are open.

### Stage 1: start now, in parallel

| Batch | Work | Owns | Waits on | Size |
|---|---|---|---|---|
| B2-0 | **The first PR of the plan.** Move the Console route fall-through, the hook arm, `pair()` and the `/mcp` mount out of `http.rs` into per-family modules, with no behavior change; the 501 list stays exactly `routes.json`. Open it before any other batch touches `http.rs`. | `http.rs`, new `rust/daemon/src/routes/` modules | none | 1 |
| B1 | Graph and dream engine, tasks L to P, in flight in the Rust port session. L: `memory_consolidate` (#836, open). M: graph store writes (`memory_graph_relate`, `memory_graph_unrelate`, `memory_alias`, `memory_relation_define`): landed, #842 in batch #866. N: graph reads: N1 `memory_graph` traversal (#847, open, stacked on #842), then N2 `memory_recall` with graph expansion plus the decision record. O: `memory_graph_review` (queue, merge, duplicate and junk proposals, fold direction, vetoes, three-judge path, audit). P: `memory_dream` and deep dream: P1 the consolidation pass (#864, open) and the outcome, lesson and digest stages (#867, open); P2 edge re-scoring, hard-violation cleanup, link discovery and the graph-insight refresh (P2b under Option B, a structurally compared declared divergence); then the automatic dream and graph-sweep ticks. Also the credential-bearing extractor and recall redirect refusal. | graph, dream and recall modules under `rust/daemon/src/`; `mcp/dispatch.rs`; `daemon/harness/graph_*.py`, `w2e.py` scenarios; PARITY MCP rows; the graph rows of `divergences.md` | none (#833 landed) | 6 to 9 (L 1, M 1, N 1 to 2, O 1 to 2, P 2 to 3); 1 landed, 4 open |
| B3 | Hooks: `/api/briefing`, `/api/hook/session-start` and `/api/hook/memory-policy` (briefing composition, session-start registration, policy logging, the release offer), the authorized memory-changes note and the session-end close. | the hook arm of `http.rs`, a new briefing module, hook cases in `daemon/harness/run.py` | B2-0 | 2 to 3 |
| B5 | Board blockers: W2F-SHARED-HLC (board onto `Ready::tick_hlc`), W2F-PAIRING-HTTP (two-slot gate, refunds, disconnect ownership, principal-cache publication; CLI-PAIRING-ENDPOINT), then W2F-CLOCK-RETRY once B4's writer-session refresh lands. | `rust/daemon/src/board/`, `hlc.rs`, `principals.rs`, `auth.rs`, the `pair()` handler; board, clock-retry and security harness cases | B2-0; B4 for the clock retry | 3 |
| B6 | Tokenless MCP Host/Origin rebinding: wire the allowlist into the tokenless mount, then run the rebinding cases with a tokenless daemon pair. | the `/mcp` mount (in `http.rs` until B2-0), `mcp/mod.rs`, `mcp_ci.py`, `mcp_compare.py` (queued with B1), `spec-http-security.md` | B2-0; D3 to accept | 1 to 2 |
| B7 | Documents. Python first: the pgvector document table (a schema bump with the full seven-place checklist and the Rust schema plan regeneration), its ingest and search, and the Chroma move (D5). Then Rust: `document_ingest`, `document_search`, the reference pool in `/api/search` and `memory_search`, and the reference counts in `memory_stats`. | `schema.py` and the Python document code; the Rust schema plan; new Rust document modules; `mcp/dispatch.rs` (after B1's current PR) | D5 | 4 to 6 (Python 1 to 2, Rust 3 to 4) |
| B8 | Models and image: provision the verified Qwen embedding and reranker exports (blockers 6 and 7), ONNX-PREREQUISITE, the bf16 scope, and an image CI job that proves the graph. | `ops/Dockerfile.daemon-rust`, `ops/provision_embedding_models.py`, `onnx_artifacts.rs`, `embed.rs`, `daemon/harness/embedding.py`, `spec-w3j-container.md` | D4 | 2 to 3 |
| B10a-d | CLI remainder, split by blocker; each mode lives in its own `rust/shim/src/cli/<mode>` and `rust/cli_harness/rows/<row>`, so the four can run in parallel. a: the daemon-container `docker exec` transport (audit, invite, maintainer). b: native `ensure_schema` for import (CLI-TRANSFER-SCHEMA), audit stats and local-zone time parsing. c: `doctor --disposable-proof`, `connect` for Codex and pairing codes. d: `tunnel` runtime, `expose`, and the `move` itself. | per-mode CLI modules and harness rows; their PARITY rows | a needs nothing; the lite halves wait on B9 | 8 to 10 (a 1 to 2, b 2, c 2, d 3 to 4) |
| B14a | Divergence triage: classify every declared divergence as closed, accept-as-is (for D7) or owned by a batch, as a docs-only PR. | `divergences.md` and the spec divergence tables (row text only) | none | 1 |
| B15 | Configuration and environment appendix: account for each section, field and variable below, or record an accepted disposition (CONFIG-LOAD). | `rust/daemon/src/config.rs`, the appendices in this file | none | 2 to 3 |

### Stage 2: after the first services land

| Batch | Work | Owns | Waits on | Size |
|---|---|---|---|---|
| B2a | Console read projections: 14 of the 15 `w2d-console-projections` GETs (all but `/api/config`, which B2d takes), the standalone canonical-search `/api` shapes, the `/api/agents` default view and the three maintainer GETs. Under proposal A, `/api/stats` moves to the CLI batches. | `routes/` read modules, `rust/daemon/src/read/`; `w2d_*.py` harness | B2-0 | 3 to 4 |
| B2b | Console writes over bodies already served on MCP: facts set, resolve and forget, reinforce, delete, supersede, consolidate, the five episode routes, and the lesson and world restores (the restore path first). Under proposal A the five episode routes and the two restores leave this batch (retired, or `episode/start` and `episode/end` served for the CLI in B10). | `routes/` write modules; REST scenarios in `w2e.py` | B2-0; #836 for consolidate | 3 to 4 |
| B2c | Graph, review, curation and dream routes: 13 GETs and 15 POSTs over B1's services; under proposal A, 10 GETs (no `/api/curation/retired`, `/api/graph/communities`, `/api/graph/proposal-evidence`). | `routes/` graph modules | B1's matching task | 3 to 5 |
| B2d | Maintainer POSTs (7) and `/api/config` GET and POST, behind the operator gate. | `routes/` maintainer and config modules | B2-0 | 2 to 3 |
| B4 | Durability and background: the writer-session probe and reconnect, autosave and the clean-exit flush (D2), compaction mutation with slot sync, the reaper's automatic titles, the unemitted `/health` keys (`stall`, `migration_partial`, `capacity_warning` and the rest), and the shutdown deadline. | `service.rs`, `background.rs`, `background_sessions.rs`, `maintenance.rs`, `health.rs`, resident stores | D2 for autosave; serialize with B1-P on `service.rs` | 3 to 5 |
| B9 | Storage tiers, per D1. If lite is ported: native embedded-PostgreSQL lifecycle (start or attach, first-boot lock, major-version refusal, Windows non-ASCII guard, per-user data dir), the daemon's storage resolution, then the lite halves of CLI-BACKUP-LITE, CLI-TRANSFER-LITE, CLI-AUDIT-TRANSPORT, CLI-PAIRING-CONTAINER, CLI-MAINTAINER-TRANSPORT and the lease's embedded refusal, with native Windows daemon runs (lite runs the daemon on Windows). If embedded and file mode retire: a deprecation release first, then their removal, keeping a `.pt` import. | a shared embedded-PG module, daemon startup storage resolution, the CLI transports | D1 | 3 to 7 |
| B10e | Operator CLI modes, per D8: `maintainer setup`, `coordination-recovery`, `channel`'s remainder, `delegate-watch`. | per-mode CLI modules | D8 | 2 to 3 |

### Stage 3: release path

| Batch | Work | Owns | Waits on | Size |
|---|---|---|---|---|
| B11 | Ops cutover: every OPS-CUTOVER, OPS-IMAGES, OPS-INSTALL-RESTORE and SHIM-CLIENT-UPDATES item below. That covers the opt-in Rust compose file, the container contract, release and checkout `update`, the installers, backup and restore, unattended updates, and client runtimes. The shim's `update` mode lands here. The installers do the whole setup themselves (CLAUDE.md "Install and update experience"). | `ops/`, `pseudolife_memory/update_cli.py` and the installers, `rust/shim/src/cli/update` | B7, B8, and B9 per D1 | 6 to 10 |
| B13 | Hosted evidence: run the body differentials (`w2e.py`, `w2d_search.py`, `board/run.py`) in hosted CI on both OSes, through portable goldens (`w2d-portable-goldens`) or a provisioned real-model oracle; add Windows daemon runtime runs if D1 keeps lite; fix the stale tokenless `woke` case in `daemon/harness/run.py` (declared 501, now served). Under proposal C the differentials run nightly on a Linux dev box with published JSON instead, and this batch keeps only the `woke` case, the Windows runtime runs if D1 keeps lite, and whatever hosted comparison D6 still requires: 1 to 2 PRs. | `.github/workflows/rust.yml`, `rust/ci/`, harness goldens; under proposal C a dev-box nightly script and its published results | D6 and D11 for scope | 3 to 5 (1 to 2 under proposal C) |
| B14b | Divergence acceptance: the maintainer accepts the residue from B14a after the other batches close their rows (D7). | `divergences.md` | every other batch | 1 |
| B12 | Docs cutover: every DOCS-CUTOVER item below, once B11 fixes the installed entry points. | `docs/`, READMEs and translations, `llms*.txt`, atlas | B11 | 2 to 3 |

### Stage 4: shadow, soak, switch and freeze (maintainer-gated)

| Batch | Work | Gate | Size |
|---|---|---|---|
| B16 | Shadow and soak on the live bank. First, restore the newest live backup into a disposable bank and replay recorded read traffic (search, fact and lesson reads) through both daemons, comparing answers. The Rust daemon is a writer even when it only serves reads: it takes the bank's writer lease, searches log retrieval events and access counts, and background sweeps prune logs and close idle sessions. Beside the live daemon it starts `not_ready` and keeps retrying (`rust/daemon/src/storage/mod.rs`, `LeaseHeld`; `service.rs`, `ensure_storage`), so it would take the writer lease the moment the live daemon let go of it, for example during a restart or an update. It must not run against the live DSN until a read-only mode exists. A live shadow therefore needs a read-only mode the daemon does not have yet (no lease, no telemetry, no sweeps, a read-only PostgreSQL role); build it, then compare its reads with the Python daemon's on the live bank. Then soak Rust as the maintainer host's live daemon behind a rollback tag, watching health, the board and dreams. | G1 | 2 to 3, plus the soak window |
| B17 | Default switch, release and freeze: installers and release-mode `update` select the Rust daemon (G2); release on the four surfaces (G3); Python daemon development freezes to fixes only (G4; under proposal B that half is already in force). The Python oracle, its tests and the eval harnesses stay until a separate retirement decision. | G2, G3, G4 | 1 to 2 |

Total: about 65 to 95 PRs (1 to 2 fewer under proposal A, 2 to 3 fewer under proposal C). The
longest chains are B7 (the Python document table, then the Rust document
tools, then the image), B1 then B2c, and B4 then B5's clock retry. All three
feed B11 and B13, then B16.

### What sets the pace

The PR count is not the constraint; writing the code is the cheap part. Each
PR costs two reviews (an Opus reviewer and the Codex second review), their
fixes and re-review, and then a hosted CI pass of about an hour, with the
Windows parity shard the long pole; until 2026-10-19 every master move also
sent every open PR back through CI. So the pace is sessions × review × CI
queue: how many Rust sessions run at once without colliding on the hot-spot
files above (three or four), how fast each PR's two reviews turn around
(hours when a reviewer session is available), and how many CI passes a day
the gate takes. Measured: between 2026-10-18 00:13Z and 2026-10-20 01:25Z (about 49
hours) master took 27 merged PRs, plus the 10 members that batch #850
landed, which GitHub shows as closed rather than merged; the two integration
batches #850 and #866 landed 12 member PRs between them. 16 PRs were open at
this revision.

Three changes since 2026-10-19 widen that queue: integration batches (#851)
make one CI run land several PRs, the CI reshape in batch #868 (merged as
72fbcc88) cuts the required hosted Windows lanes to what differs on Windows, and the
4-worker
local suite (#846) gates a batch head in about ten minutes before CI sees
it. The calendar estimate that follows is an estimate, not a commitment, and
moves with the session count: at one reviewed PR per session per day, three
to four sessions and two batches a day, 65 to 95 PRs is about three to six
weeks of calendar time to the end of stage 3, with the longest chain (B7,
then B8 and B11, then the B16 soak) as the floor the count cannot beat.
Stage 4 is maintainer-gated and adds the soak window.

Measured per batch, 2026-10-23, over every integration batch opened since
2026-10-19 ([`evals/results/batch-lifecycle-20261023/`](../evals/results/batch-lifecycle-20261023/README.md),
`python ops/batch_timeline.py`): 8 merged batches (n=7 before #890's
batch-only heavy CI, n=1 after it) and one closed and rebuilt. Before #890,
a member's median time from opened to `ready-to-batch` was 7h47m (n=21), of
which 4h38m came after its last push; from the label to merged it was
2h35m. Inside that, a batch's CI first pass took a median 1h17m (n=7), of
which 15m07s was runner wait on the critical path; local validation took
10m09s and the build seconds. Every merged batch's critical path ran through
a Windows parity shard (8 of 8). The one post-#890 batch (#892, n=1) took
1h16m from its PR to merged, 15m43s of it a rerun of a macOS lane the
ruleset does not require, which no hosted runner had acquired. So the wait
before a change joins a batch, which includes its review, is the largest
measured cost; GitHub holds no review event here, so it cannot be split
further. One post-#890 batch is not a trend; rerun the report as batches land.

## Acceptance checklists

The tables below are the 2026-10-10 draft's acceptance rules. The plan above
schedules them; it does not change them.

Source audit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`, 2026-10-10.
All five scoped rows in [PARITY.md](PARITY.md#parity-rows) are **deferred**.
Every item below is **pending cutover acceptance**, including items whose
Python implementation or static guard already exists. A source reference is
not an execution result. Record later evidence against the actual tested head,
command, platform, exit code and CI run; do not reuse an older head's proof.

## Acceptance boundary

| Item | State | Evidence needed / current authority |
|---|---|---|
| Whole-row closure | Pending | Close every contract item before changing a PARITY row to ported; otherwise add only the evidence actually obtained. |
| Disposable installed paths | Pending | Fresh install, update and restore through the actual installed Rust entry points; compilation and Python fakes do not establish installed-runtime parity. [Phase 5 gap](PARITY.md#per-phase-selection-and-acceptance-gaps). |
| Differential acceptance | Pending | Reuse `daemon/harness/run.py`, `compare.py` and `dbstate.py`: response values and consumer-read headers, touched and potentially touched DB rows after writes, and files in isolated client homes. Retain declared normalizers, raw failures, and caught source mutants. |
| Platform and hosted gates | Pending | Linux disposable proofs, native Windows evidence for OS-sensitive client/process/file behavior, and completed exact-head Rust/Parity CI on both OSes; required repository guards and current-master integration. |
| Independent reviews | Pending | Required independent reviews and findings/dispositions in the PR body; a fixed finding receives a covering case and watched passing rerun. |
| Remaining divergences | Pending | Reconcile all port slices' declared divergences; the union must be empty or explicitly accepted by the maintainer. Existing unsupported embedded, document, model and client paths are not silently retired. |
| Maintainer execution | Pending | Default switch, release, update behavior changes, live-bank mutation and deploy remain separate maintainer decisions. Keep the Python oracle until its retirement is explicitly decided. |

## OPS-CUTOVER

Current sources: `ops/Dockerfile.daemon`, both compose files in `ops/` and their
copies in `pseudolife_memory/compose/`, `pseudolife_memory/update_cli.py`,
`unattended_update.py`, `runtimes.py`, and `.github/workflows/`.

| Item | State | Evidence needed |
|---|---|---|
| CPU image | Pending | Inspect built Rust image/runtime dependencies and execute on CPU with GPU hidden; retain Python CPU guards (`test_daemon_image_is_cpu_only.py`). The Python lock/install assertions alone do not inspect Rust dependencies. |
| Offline image and models | Pending | Cold start and authenticated model-dependent read without runtime downloads. Python bakes Qwen3 and MiniLM; Rust uses a verified Qwen3 ONNX export and dynamic ONNX Runtime (`daemon/src/embed.rs`, `daemon/Cargo.toml`, `daemon/harness/run.py`). Package tokenizer/config/artifacts and library paths; do not treat an ONNX graph as proof of all Python model/fallback contracts. |
| Image ONNX graph agreement | Blocked for cutover | The Rust image's ONNX graph is not the verified export; numerical parity unproven. Public export `onnx-community/ONNX_Qwen3-Embedding-0.6B` at `462e5a71e724575c710975d9b79309b690fd22ce` has graph SHA256 `cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b`; the historical prerequisite uses `f5fdee679d0ffb98e1cb0bb178e8ee2b86739ab7ab035a3a9327761d5c6b1f0b`. External fp32 tensors and tokenizer match. Before switching defaults, bake the verified export or prove top-8 ranking overlap mean ≥ 0.95 with no query below 0.75; until then keep this image opt-in. |
| Sent YAML presentation deferrals | Pending disposition | `sent-quoted-continuation-tab` (both quote styles, continuation starts with a literal tab at column zero) and `sent-anchor-colon-terminator` (`a: &x:y b`, PyYAML names the anchor `x`) are accepted by PyYAML and refused by the native sent loader. Shipped producers use space indentation, escaped tabs and ordinary ASCII anchor names. Preserve these named refusals or obtain an accepted canonical scope disposition before closing the boundary; differential cases are in `daemon/harness/test_sent_yaml.py`. |
| Container runtime contract | Pending | Compare entry point, runtime user, bind/auth guard, port 8765, `/data` persistence/config, build stamp and health check. `ops/Dockerfile.daemon` has no explicit `USER`; do not invent an existing non-root guarantee. |
| Compose byte equality | Pending | Preserve `ops/docker-compose.yml` and `docker-compose.ghcr.yml` byte-for-byte against their bundled copies; run `test_update_cli.py::test_bundled_compose_files_match_the_checkouts`. An opt-in Rust file must leave the default stack unchanged. |
| Compose resources and isolation | Pending | Disposable side-by-side run on a separate port/bank: loopback publication, own project/resources, PostgreSQL healthy dependency, external production volume semantics, `/var/lib/postgresql` PG18 mount, daemon memory/swap cap, log rotation and internal-only extractor. Verify only owned disposable resources are cleaned up. |
| Backup before update | Pending | Real disposable dump plus state archive, complete-dump marker, failure/refusal before movement, correct checkout/bundled backup selection and original env/volume identity. `test_update_cli.py`, `test_ops_backup_integrity.py`. |
| Rollback before movement | Pending | Tag the running validated image, preserve collision/refusal rules, demonstrate rollback instructions and disposable recovery after unhealthy or version-mismatched replacement. `test_update_cli.py`, `test_ops_prune_rollbacks.py`. |
| Daemon-only recreation | Pending | Prove `up -d --no-deps` changes only the daemon, keeping PostgreSQL, extractor and bank/state volumes; preserve checkout build stamp and release pull paths. `test_update_cli.py`, `test_ops_update_wrappers.py`. |
| Health before clients | Pending | Successful expected-version health gate before client movement; unhealthy/mismatched health exits failure and moves no clients. Exercise the changed authenticated path, not health alone. Health appendix below remains required. |
| Release versus checkout update | Pending | Preserve newest/pinned release, downgrade/refusal and no-op semantics; checkout clean-tree/build/rollback safeguards and `--all`; `--daemon-only`, `--clients-only`, pip/lite/client-only routing and wrapper exit codes. Preparation changes no updater behavior. |
| Client completion and failures | Pending | Actual shim runtime, plugin cache and supported hook/client steps; daemon success plus client failure remains a failed client step (current updater exit 5). Preserve consent/ownership and useful next-command diagnostics. `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_update_cli.py`. |
| Runtime beside current sessions | Pending | Install/select a complete new runtime while an old session keeps working; next session uses the new runtime; keep running, registered and frozen-tunnel runtimes; fail closed on unreadable process/tunnel evidence. `test_shim_runtimes.py`. |
| Unattended daemon upgrade policy | Pending | Opt-in knob plus no active board session, readable board and update lock; unavailable/unknown board holds off; idle sessions retain runtimes. Preserve notices/logs and exits 0 updated, 3 current, 4 held, 2 unable to check, 1 failed. `test_unattended_update.py`, `unattended_update.py`. |
| Unattended client policy | Pending | Opt-in installed-runtime launch, correct daemon release target, one concurrent attempt and retry interval/result persistence; first successful frame and cancellation ownership. `test_update_offer.py`, `test_shim_runtimes.py`; see SHIM-CLIENT-UPDATES. |
| Fresh install/update UX | Pending | Detect host/tier/provider, finish chosen setup automatically, rerun safely, skip interactive work without hanging when unattended, and refuse before partial mutation. Contributor test-login setup stays confined to selected contributor paths. `CLAUDE.md`, `test_client_install_ux.py`, `test_client_credentials_setup.py`, `test_ops_script_modes.py`. |
| GitHub release surface | Pending | Future maintainer-controlled release procedure, version/notes/artifacts and release gate; current `.github/workflows/release.yml` and `.claude/skills/release-procedure/SKILL.md` remain authority. No release workflow change or publish in preparation. |
| PyPI surface | Pending | Future installed package/entry-point proof, version agreement and completed human-approved publisher evidence. Preserve source/packaging inspected by existing guards. |
| MCP registry surface | Pending | Future `server.json`/package identity/version agreement, registry metadata and served-version verification under the existing release procedure. |
| Plugin marketplace surface | Pending | Future manifest/marketplace/hooks currency, cache identity/digest and new-session adoption while current sessions retain their installed code. |
| GHCR image artifacts | Pending | Future maintainer-authorized versioned daemon/PG artifacts and inspectable revision/build metadata; preparation CI builds only and never pushes. GHCR images are additional release artifacts, not a substitute for any of the four public surfaces above. |

## OPS-IMAGES

| Item | State | Evidence needed / current source |
|---|---|---|
| Extractor model | Pending | `ops/Dockerfile.extractor`: baked default GGUF, optional `MODEL_URL`, runtime model path `/models/extractor.gguf`; inspect artifact and run disposable CPU/offline serving proof. Preserve default/model currency guards. |
| Extractor config | Pending | OpenAI-compatible CPU server, bind `0.0.0.0`, internal port 8081, context 8192, Jinja, parallel 1 and idle sleep 300/default override. Dockerfile CMD and compose command stay aligned (`test_extractor_idle_sleep.py`). |
| Extractor health | Pending | Compose overrides inherited health to `/health` on 8081; health answers while sleeping without waking the model; next completion reloads. Disposable startup/sleep/completion evidence, not model-list guards alone. |
| PostgreSQL build/runtime | Pending | `ops/Dockerfile.pg`: `pgvector/pgvector:pg18`, OS package updates, PostgreSQL 18 and pgvector availability; disposable init, vector access, persistence/recreate and dump/restore compatibility using intended PG18 mount. |
| Daemon CPU/offline | Pending | All OPS-CUTOVER image/model/runtime items above, Python image left intact, built Rust image inspection and network-independent execution. `test_daemon_image_is_cpu_only.py`, `test_release_ux.py`. |
| Compose and image selection | Pending | Byte equality and preserved release/local image semantics; no default Rust image selection. `test_update_cli.py`, `test_extractor_model_lists.py`. |
| Disposable image CI | Pending | Exact-head no-push build/start, disposable PostgreSQL, healthy `/health`, authenticated read and owned-resource cleanup; retain successful run URL and logs. Existing `.github/workflows/rust.yml` is the integration point. |

## OPS-INSTALL-RESTORE

For each script, preserve its canonical flags, preflight checks, refusal/exit
codes, rerun behavior, rollback and platform/client ownership. Exercise real
scripts with disposable homes, banks and stubbed platform registration where
needed; static assertions and Python fakes remain oracle evidence.

| Script item (`ops/` relative) | State | Evidence needed / guard |
|---|---|---|
| `install.sh`, `install.ps1` | Pending | Fresh tier/provider selection, complete setup, noninteractive/rerun behavior, credentials, runtime selection and fallbacks; `test_client_install_ux.py`, `test_client_credentials_setup.py`. |
| `install-autostart.ps1` | Pending | Selected daemon/interpreter/data/DSN/environment and Windows task ownership/start behavior in an isolated platform fixture. |
| `install-shim-autostart.sh`, `install-shim-autostart.ps1` | Pending | Claude extractor CLI/interpreter/model/prompt, bind verification, replacement/restart and sibling mode cleanup; `test_client_install_ux.py`, `test_extractor_model_lists.py`. |
| `install-codex-shim-autostart.sh`, `install-codex-shim-autostart.ps1` | Pending | Codex counterpart model/health/interpreter and sibling cleanup with matching platform behavior; same guards. |
| `install-hook.sh`, `install-hook.ps1` | Pending | Supported client hook store, prompt/session wiring, trust-review consent and ownership; preserve definitions users approve. `test_client_install_ux.py`. |
| `restore.sh`, `restore.ps1` | Pending | Disposable rehearsal and explicit apply; safety dump, drop/create failures, close PUBLIC before replay, every-table loss checks, held-dump selection/refusal and no-start semantics. `test_ops_restore_rehearsal.py`, `test_ops_restore_skips_held.py`. |
| `restore_from_pt.py` | Pending | Additive same-era snapshot restore, retain existing rows/source backups, stopped-daemon rehydration and dimension refusal before connection/write. Python `.pt` support is not retired by this draft. |
| `preflight.sh`, `preflight.ps1` | Pending | Selected providers/tier/CLI/pipx checks, official install layouts and useful refusals; `test_client_install_ux.py`. |
| `migrate-pg18.ps1` | Pending | Disposable dump/restore/verification across source major, target PG18 layout, retained old-volume rollback and failure paths; no live migration. |
| `backup.sh`, `backup.ps1` | Pending | Complete dump and state tar, marker/integrity checks, row-count manifest/baseline/drop hold, mirror/rotation safety and daemon backup record; `test_ops_backup_integrity.py`. |
| `install-backup-task.ps1` | Pending | Main/script checkout selection with output kept in main backup directory, installing-user task, absolute PowerShell path, missed-run behavior, log/HEAD and nonzero failures; `test_ops_install_backup_task.py`. |
| `prune-rollbacks.sh`, `prune-rollbacks.ps1` | Pending | Keep newest configured count, deployed/dangling/in-use protection and quiet no-op; `test_ops_prune_rollbacks.py`. |
| `prune-build-cache.sh`, `prune-build-cache.ps1` | Pending | Age/cap retention, repeated measured progress with budget/no-progress stop, dry-run, safe Docker verbs, optional Docker Desktop trim; `test_ops_prune_build_cache.py`. |
| `install-cache-retention.ps1` | Pending | Absolute/stable PowerShell executable including MSIX alias, correct encoded script/retention parameters and task scope; `test_ops_install_cache_retention.py`. |
| `register_claude_desktop.py` | Pending | Own-entry merge/migration, preserve foreign entries/settings, absolute launcher and packaged Windows config path, owner-only token-file handling and capability probe/refusals; `test_register_claude_desktop.py`, client credentials/UX guards. |
| `setup-codex-coordination.py` | Pending | Effective config/identity authority, opt-in messaging and wake capability, private backup and preserved preexisting settings; real installed client with disposable config. |
| `update.sh`, `update.ps1`, `update.py` | Pending | Every canonical flag/default maps to the checkout updater, streamed output and exact exit status; `test_ops_update_wrappers.py`, OPS-CUTOVER update sequence. |
| `update_clients.py` | Pending | Separate runtime/plugin/hook/provider install steps, owned cache/config, effective credentials and failed-step exit; actual installed-runtime proof with old sessions present. |
| Executable script modes | Pending | All tracked `ops/*.sh` retain index mode 100755 for Linux/macOS direct invocation; `test_ops_script_modes.py`. PowerShell has a separate native parse/runtime proof. |

## SHIM-CLIENT-UPDATES

| Item | State | Evidence needed / current source |
|---|---|---|
| Installer-backed unattended execution | Pending | Real installed client starts the intended updater after its first successful frame, only under configured policy; captures process ownership, concurrent startup, crash/failure/retry and persisted results. `unattended_update.py`, `client_updates.py`, `shim.py`, `test_update_offer.py`. |
| Installed runtime selection | Pending | Correct launcher, newest complete runtime marker and dependency closure; incomplete install is not selected, running/registered/tunnel-pinned runtimes survive pruning. `runtimes.py`, `test_shim_runtimes.py`. |
| Complete client installation | Pending | Actual package install, supported Claude Code/Codex/Claude Desktop/Gemini/generic provider registration, daemon URL, no-spawn and writer settings, plugin/hooks, credentials and next-session handshake. Dependency-light binary compilation alone is insufficient. |
| Client environment isolation | Pending | Remove inherited credentials/connections before importing test helpers; disposable home and explicit endpoint; owner-only files and Windows ownership/locale behavior. `test_client_environment.py`, `test_client_credentials_setup.py`. |
| Update offer/defaults | Pending | Check enabled by default, unattended clients/daemon off by default, valid config and release notices, installed command/launcher name and quiet current/unknown behavior. `test_update_offer.py`, `utils/config.py`. |
| Configuration ownership/recovery | Pending | Backup/preserve unrelated registrations and user credentials; refuse bad input without half-written state, keep current sessions usable and next ordinary operation successful after faults. `test_shim_runtimes.py`, `test_client_install_ux.py`. |

## DOCS-CUTOVER

| Item | State | Evidence needed / current source |
|---|---|---|
| Guides | Pending | Reconcile `docs/guide/` install, update, recovery and usage instructions with the accepted installed entry points; links/commands exercised on disposable state. |
| Providers | Pending | `docs/guide/providers.md` matches installers' supported client/transport/hook capability matrix, effective config, credential and ownership behavior; `test_client_install_ux.py`. |
| Tunnels | Pending | Guide/current tunnel runtime and CLI agree on forwarding, bridge/runtime pinning, auth, setup/refusals and recovery; named unsupported modes stay visible. |
| Configuration | Pending | Every section/field/default, missing/null/type validation and YAML/env precedence; all configuration/environment appendix items below accounted for. `utils/config.py`, `docs/guide/configuration.md`. |
| Schema | Pending | Current code version, README capability table, configuration DSN/history, schema pins/migration assertions, changelog and atlas agree. No schema bump is made here; follow all seven-place requirements if one later lands. `test_release_ux.py`, `test_schema_version.py`, `test_migrate_embeddings.py`. |
| System Atlas | Pending | Verify actual affected storage/runtime cards before updating metadata; `docs/atlas/atlas.json`, `test_atlas_currency.py`. |
| READMEs and translations | Pending | Accurate install/default/runtime/schema/capability claims across README and `docs/i18n/README.*.md`; `test_release_ux.py`, `test_i18n_readme.py`. |
| Defaults and model lists | Pending | Embedding/backend/fallback, extractor modes/models, preset/toolset and unattended defaults agree across source, menus, help and guides; no unmeasured default/recommended promotion. `test_extractor_model_lists.py`, `test_release_ux.py`. |
| Installed help and manifests | Pending | Actual installed command/help/schema/environment markers, `server.json`, `pyproject.toml` and release manifests describe shipped behavior; retain packaging/source until retirement decision. |
| LLMS generation | Pending | Regenerate with `python ops/gen_llms_txt.py` after covered doc edits, inspect `llms.txt`/`llms-full.txt` and run `test_llms_txt.py`; generation is not assumed from writing this draft. |
| Benchmark claim currency | Pending | Claims retain committed artifacts, instrument/version/retirement provenance and appropriate claim guards; no new benchmark or performance claim here. `test_eval_evidence.py`, CLAUDE.md benchmark rules. |
| Repository/publication guards | Pending | `test_repository_claims.py` remains the named coordination repository-claim guard; run all row-named guards without treating it as benchmark evidence. Inspect actual public diff/metadata and publication guard before publishing. |
| Oracle retirement | Pending | Explicit maintainer decision after installed/differential acceptance and divergence reconciliation; retain Python oracle tests, source and packaging before that decision. |

## Appendix acceptance dependencies

The configuration and environment inventories below enumerate the current
PARITY appendix names. They retain the canonical section/reader paths in
[Configuration sections](PARITY.md#configuration-sections) and
[Environment variable appendix](PARITY.md#environment-variable-appendix),
including field-level inventory in `phase1-contract-inventory.json` and reader
paths in `contract-inventory.json`. Every listed name is pending accounting
for its installed Rust consumer or an explicit maintainer-accepted disposition;
this list does not claim that every internal test variable belongs in the image.
Compare declared fields/reader paths, defaults, validation and missing/YAML/env
precedence rather than porting arbitrary Python parser behavior.

### Health and HTTP

State: **Pending**. Source: [health/body appendix](PARITY.md#health-http-bodies-and-logical-transfer),
`pseudolife_memory/daemon.py`, `web/api.py`, `rust/daemon/src/health.rs`.
Prove mandatory `status`, `version`, `schema`, `storage`, `auth`, `bank`,
`persist_errors`, `memory`; and conditional `build`, `coordination`, `updates`,
`extractor`, `stall`, `hooks_digest`, `init_refusal`, `not_ready`,
`migration_partial`, `dream_tracking_error`, `capacity_warning`,
`lesson_reconciliation_required`, `embedder`, `db`, `last_backup`.
Preserve 200 for ok/absent status, 503 for other payload status, exception 500,
the source degradation-versus-warning distinction and null bank fingerprint.

Nested items remain pending individually: `build.{git_sha,dirty,built_at,source}`;
`embedder.{backend,device,dtype}`; `last_backup.{at,age_hours,rotation}`;
`coordination.enabled` and `coordination.wake.{per_recipient_per_hour,urgent_per_sender_per_hour,nightly_total,fan_out_stagger_seconds,active_seconds}`;
`updates.{check_releases,latest_release,checked_at,unattended_clients,unattended_daemon}`;
`stall.{since,reason,consecutive_failures,last_success_at}`;
`memory.{source,current_bytes,working_set_bytes,limit_bytes,used_fraction,near_limit,events,anon_bytes,file_bytes,rss_bytes,rss_peak_bytes}`
with `events.{max,oom,oom_kill}` and absent versus null preserved.

HTTP acceptance remains pending: Console POST 262144 bytes except facts/set,
consolidate and supersede 4194304; coordination 32768; session-end 16384;
pair 1024; GET hooks and woke/subagent hooks use query/header inputs;
`/api/agents?view=coordination` projection, unknown routes and method errors.
Container health smoke covers only the exercised subset.

### Restore and logical transfer

State: **Pending dependency**, not a new port in this preparation. Physical
backup/restore and `.pt` recovery must remain distinct from logical transfer.
The [transfer appendix](PARITY.md#health-http-bodies-and-logical-transfer)
requires every exported/excluded table, `outcome_signals.used_ids`, bank-local
metadata and target emptiness rule to be preserved. Include them in disposable
export/import/restore state checks, not just counts.

Every exported table is pending: `meta`, `episodes`, `entries`,
`entry_reinstatement_decisions`, `entities`, `entity_aliases`, `relations`,
`edges`, `edge_evidence`, `edge_proposals`, `entity_proposals`, `entity_kinds`,
`dismissed_pairs`, `facts`, `world_facts`, `lessons`, `outcome_signals`,
`communities`, `entity_communities`, `memory_traces`,
`memory_trace_invalidations`, `entity_sources`, `merge_decisions`,
`chronicle_events`, `curation_judgments`, `store_decisions` (26).

Every exclusion is pending: `dream_runs`, `dream_run_slots`,
`retrieval_events`, `retrieval_uses`, `slot_reads`, `lesson_search_events`,
`client_sessions`, `coordination_agents`, `coordination_messages`,
`coordination_events`, `coordination_leases`, `coordination_lease_waiters`,
`coordination_wakes`, `principals` (14); `outcome_signals.used_ids`;
metadata `schema_version`, `active_session_pointer`, `dream_ack_secret_v1`,
`coordination_hlc_highwater`, `coordination_bank_id`, `writer_lease_epoch`
and every `*_schema_version` key. Target exported tables must be empty
except `meta` and builtin `relations`; excluded runtime tables never travel.

Import must delete the target's `curation_listing_spelling_v2` before importing
exported metadata so the export's value survives. Native closure of these
dependent rows is required or needs an explicit accepted disposition.

### Configuration inventory

| Section | State | Evidence needed |
|---|---|---|
| `EmbeddingConfig` | Pending | 9; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MIRASBandSpec` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MIRASConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ReferenceConfig` | Pending | 5; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `NLIConfig` | Pending | 4; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `BM25Config` | Pending | 7; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RerankerConfig` | Pending | 5; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `DreamConfig` | Pending | 41; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `DeepDreamConfig` | Pending | 49; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CortexConfig` | Pending | 11; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `LessonsConfig` | Pending | 11; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RetrievalLogConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CompactionConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MetaFilterConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `GraphInsightConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `TracesConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ScopesConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `RecallConfig` | Pending | 12; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `SearchConfig` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `McpConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MemoryConfig` | Pending | 29; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `ContextConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `TimeConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `StorageConfig` | Pending | 1; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MemoryPolicyConfig` | Pending | 2; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `WakeConfig` | Pending | 6; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `CoordinationConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `UpdatesConfig` | Pending | 4; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `AppConfig` | Pending | 8; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |
| `MaintainerConfig` | Pending | 3; see `phase1-contract-inventory.json`; field inventory/default/validation/precedence proof or accepted disposition. |

### Environment inventory

Each variable's reader/reference paths are the corresponding exact row in the
PARITY environment appendix; no values are copied into this checklist.

| Variable | State | Evidence needed |
|---|---|---|

| `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_COORDINATION` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_LABEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_PROJECT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_STATE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_STATE_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_TASK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE_HOOK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BACKUP_MIRROR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BACKUP_MIRROR_KEEP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BANK_VOLUME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BENCH_DB` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BOARD_HARNESS_NAMES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_DIRTY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_GIT_SHA` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_BUILD_TIME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_LOG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PROMPT_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CLAUDE_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_BIN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_DOORBELL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_HOOK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SERVER_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SERVER_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_HEALTH_TTL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_LOG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_CODEX_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DAEMON_EXEC` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DESKTOP_TOKENS_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DESKTOP_TOKEN_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DIGEST_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DOCKER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_BASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_EXTRACTOR_MODE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_FALLBACK_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_MAX_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_MODEL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_HANDLE_RESUME_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_IMAGE_TAG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_INSTALLER_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_INSTALLER_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_JUDGE_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_JUDGE_SECOND_API_KEY` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEASES_HELD` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEASE_LOCK_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_CONFIG` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DATABASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_DATA_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_NO_SPAWN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_SHARED_HOST` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_STORAGE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TIER_MAP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKEN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKENS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOKEN_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TOOLSET` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_MODELS_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_PLUGIN_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_PRIVATE_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_RECALL_DRIVER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_RELEASE_CHECK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_REQUIRE_TEST_POSTGRES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_IDLE_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SESSION_RESUME_SECONDS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_CLAUDE_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_CODEX_CLI` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_LAUNCHER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_RUNTIMES` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SHIM_USER_BIN` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_STATE_VOLUME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_COMMIT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_DISPATCHED` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_ENV_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_GIT_COMMON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_KEEP` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PRUNE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PRUNE_DAYS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LEASE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LOCK` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_LOCK_DIR` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_NAME` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_PYTHON` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_REMOTE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_RUN_ID` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_SLOTS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_VENV` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_WHERE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_SUITE_WINDOWS` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_CUDA` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_DATABASE_URL` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_EMBEDDER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_HOST_PORT` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_LOGIN_FILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_PASSWORD` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TEST_PG_USER` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WRITER_ID` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WSL_DISTRO` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `PSEUDOLIFE_WSL_SUITE_SOURCE` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
| `_PSEUDOLIFE_PRODUCTION_DB` | Pending | Reader/propagation/installed-path proof or accepted disposition. |
