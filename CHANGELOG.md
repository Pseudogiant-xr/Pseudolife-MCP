# Changelog

All notable changes to Pseudolife-MCP are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (2026-08-06 — set history is deterministic under timestamp ties)
- **`memory_history` on a set-valued slot no longer depends on strict
  timestamp inequality for its ordering.** The version list is built
  member-by-member and re-ordered by a single timestamp sort, so two
  writes landing on the same clock value could let a member's removal
  sort ahead of a later member's add (surfaced as a one-off full-suite
  flake). Ties now break adds-before-removes, then by member insertion
  order — the only order the store can still attest to at equal clocks —
  and a frozen-clock regression test pins it.

### Added (2026-08-06 — events coverage audit: quantities are the bottleneck, not ordering)
- **The multi-session residual is now audited row-by-row against the
  source sessions** (`evals/results/events-coverage-audit-0806.json`;
  method and per-row quoted evidence inside). Of the 24 rag-right /
  syn-wrong rows, 19 are amount-arithmetic — they need numeric values
  combined across sessions, and the v1 events prompt strips those
  numbers at extraction ("completed my first full marathon in 4h 22min"
  served as "completed first full marathon"). Mechanism split: 7
  not-event-shaped (static facts, out of events' reach), 5 cue-miss,
  4 extraction-or-retrieval gaps, 4 quantity-stripped, 2 partial
  blocks, 2 answerer failures. The paired BEAM autopsy (same artifact)
  retires answer-time ordering synthesis: 0 of 23 served event_ordering
  rows failed by misordering correctly-served events, and 6 of 8
  regressions were abstention-suppression — a partial events block
  reads as exhaustive and the model stops using the rest of the
  context. Three changes land now, preregistered in
  `docs/superpowers/specs/2026-08-06-events-quantity-coverage-design.md`
  with **all GPU gate runs held**:
  - `has_aggregation_cue` widens to `total <quantity-noun>`, `average`,
    and `the most` (the five audited cue-miss phrasings); bare "total"
    still does not fire.
  - `--ev-variants` gains a `hybrid_ev_hdr` arm — syn content under a
    partial-record header — targeting the abstention-suppression
    regressions.
  - `evals/prompts/events_pass_v2.txt` lands as an UNSHIPPED candidate
    (v1 + a keep-quantities/exhaustiveness rule; pinned by test to stay
    v1-plus-one-rule). The shipped extractor constant remains v1 until
    v2 passes its preregistered gate.

### Measured (2026-08-06 — aggregation-cued serving: monotone but underpowered; coverage is the bottleneck)
- **The aggregation-serving gate run is recorded as an honest negative
  with a measured cause** (preregistration
  `docs/superpowers/specs/2026-08-06-aggregation-serving-design.md`;
  verdict `evals/results/aggserve-verdict.json`; 500 questions, 6
  judged arms, tag `aggserve-0806`). The rag control reproduced the
  `ev2-sep-0804` baseline exactly (delta 0.000, 0/0 flips, contexts and
  responses byte-identical on all 500). The claims-inertness and
  reconstruction validity gates missed their exact-zero bars at −0.006
  and −0.004 via the same llama-server extraction-stream noise floor
  the BEAM run measured the same morning; the reconstruction check is
  nonetheless structurally exact — zero prefix-property violations over
  500 rows, so the within-run variant deltas are unconfounded. The
  primary (full-list + tally `hybrid_ev_syn` vs reconstructed
  `hybrid_ev` on multi-session, n=133) came in direction-consistent but
  underpowered: +0.038 (p 0.123, 6W/1L), with the decomposition
  attributing the effect to serving (+0.030, 4W/0L) over the tally
  line (+0.007). The multi-session ladder is monotone — hybrid 0.376,
  +events 0.398, +widened serving 0.429, +tally 0.436, vs rag 0.504 —
  and non-inferiority passed both halves, with the four strong types at
  exactly zero flips (n=234): serving up to 30 events on aggregation
  cues is measurably harmless where it doesn't help. The
  decision-relevant residual: of 24 multi-session rows where rag is
  right and syn still wrong, the events block is absent on 16, and
  widened serving reached only 44/133 rows against the ~95 predicted —
  the remaining gap is extraction-coverage-side. Next: a coverage audit
  of the events pass against a gold instance list, before any
  answerer-side work.

### Measured (2026-08-06 — BEAM chronicle re-run: honest negative on event ordering, temporal effect replicates)
- **The deferred BEAM gate run is recorded as an honest negative per its
  preregistered ship rule** (2026-08-06 amendment in
  `docs/superpowers/specs/2026-08-04-separate-pass-events-design.md`;
  verdict `evals/results/beam-ev-verdict.json`). BEAM 100K, all 20
  chats / 400 questions, `--chronicle`, tag `beam100k-ev-0806`, float
  rubric metric. Gate 1 (rag control vs the committed 0802 baseline):
  delta exactly 0 over 400 questions, 0/0 flips — the reproducible-judge
  pipeline reproduces bit-identically four days apart. Gate 2
  (claims-inertness) missed its exact-zero bar at −0.002 (p 0.83,
  19W/18L): the claims bank rebuilt differently on ~11 of 20 chats.
  Forensics (in the verdict artifact) attribute this to
  request-stream-composition nondeterminism of the shared llama-server
  — 1/400 *rag* responses also changed under byte-identical inputs —
  not to any events→claims code path, which measured exactly zero flips
  over 500 LME questions the day before. Under the preregistration this
  demotes gates 3–4 to within-run exploratory readings: the primary
  event_ordering gate FAILED (−0.016, p 0.68) and not for lack of
  serving — events were present on 23/40 event_ordering rows and netted
  −0.63 rubric points there, so raw dated blocks don't solve BEAM event
  ordering and answer-time synthesis is the named next experiment.
  Non-inferiority passed nominally positive: +0.020 pooled over the 9
  remaining abilities (p 0.023), driven by temporal_reasoning
  0.4625 → 0.6188 (+0.156, served on 32/40 rows) — the LME
  temporal-reasoning effect replicating in direction and concentration
  on a second benchmark, held at exploratory status by the gate-2 rule.
  No docs claim changes; chronicle stays opt-in pending the live soak
  review.

### Added (2026-08-06 — aggregation-cued event serving: counting needs the whole list)
- **Chronicle events now also serve on aggregation cues** ("how many /
  how much / how often / what percentage / in total / total number /
  altogether / each time / every time" — `has_aggregation_cue`, a
  predicate deliberately separate from `_TEMPORAL_CUE_RE`, which also
  fires the gate-failed timeline channel and must not widen). On an
  aggregation-cued query the serve cap rises from 6 to 30 (a count over
  a capped prefix is wrong by construction) and the search result
  carries `events_total` — a computed property of the served list, not
  a claimed answer. Temporal-only queries are byte-identical to before
  (same gate, same limit-6 prefix under the same ordering). Motivation
  is the `ev2-sep-0804` multi-session autopsy: 116/133 multi-session
  questions are aggregation-cued and the old gate served events on only
  21 of them, while rag led hybrid_ev 0.517 to 0.405 exactly there.
  Dead code while `memory.dream.chronicle` is off (the default);
  preregistered gates in
  `docs/superpowers/specs/2026-08-06-aggregation-serving-design.md`.

### Fixed (2026-08-05 — edge origin sticky by rank)
- **A dream re-assertion no longer downgrades a human-settled edge origin.**
  `upsert_edge`'s conflict clause took any non-null incoming origin verbatim,
  so the dream re-extracting an existing triple (`origin="agent"`) overwrote
  an edge blessed to `origin='user'` (`graph_bless_edge`) or confirmed as
  `origin='action'` (accepted review verdict) — after which the next apply's
  name-based `rescore_edges` recomputed its confidence and `dubious_edges`
  re-flagged the settled edge. Origin is now sticky by rank
  (user > action > agent): a lower-ranked re-assertion keeps the stored
  origin, upgrades and omitted-origin re-assertions behave as before.

### Measured (2026-08-05 — echo suppression is ladder-neutral; deployed)
- **The dream-write-path change in the review-autonomy work (echo
  suppression) re-ran the extraction ladder and is verdict-neutral.**
  Paired arms on the same reproducible Qwen-27B server, pre-merge master
  (`a6b9a7b7`) vs merged master (`b7934184`): `qwen-27b` rung
  gold_recoverable 1.0 / stale_leak 0.0 / 4.5 tokens-per-query on **both**
  arms; `floor` rung 0.1 / 0.1 / 3.4 on both arms. Artifacts:
  `evals/results/{floor,qwen-27b}-pr104-{pre,post}.json`. Deployed to the
  live daemon via `ops/update.ps1` (rollback tag
  `0.12.0-pre-dream-autonomy`); live verification exercised the new
  paths end-to-end — batch verdicts (`settled: 0` on stale ids), the
  `relate` verdict, and a full deep-dream apply that scope-stamped 192
  previously unattributed entities and filed 4 junk proposals, all
  correctly guard-kept.

### Changed (2026-08-05 — deep dream review autonomy)
- **The review queue no longer manufactures work a reviewer must undo.**
  Five mechanical fixes derived from the 2026-08-05 full-queue triage
  (~400 findings, ~85% settled mechanically):
  - *Junk auto-apply with a keep-guard*: `deep_dream(apply=true)` now
    deletes flagged junk entities that have no edges and at most the one
    fact slot they were minted from (the pre-apply graph snapshot is the
    undo; the node re-mints on next mention). Evidence-bearing junk stays
    a proposal. Each unattended deletion writes a durable
    `merge_decisions` audit row (`decided_by="dream-auto"`) — the
    proposal row CASCADEs away with the entity. Response gains
    `junk_deleted`.
  - *Mention-derived scope stamping*: the apply pass attributes
    still-unattributed entities from the sources of their mentioning
    entries (`memory.scopes` exclude/rollup respected) — the fact-keyed
    backfill never reached entities without a current fact, which had left
    327 entities projectless. Response gains `scoped`.
  - *Dream-echo suppression*: a fact write whose value is a strict
    compression of the slot's standing value now **confirms** the richer
    value instead of parking a contender — 4 of 7 contested slots in the
    audit were the dream re-asserting a terser copy of what the slot
    already said. Disqualified outright (conflict path preserved): any
    novel digit-bearing token, a negator on either side ("not deployed"
    -> "deployed" is an update, not an echo), or fewer than three tokens.
  - *Evidence-ranked fold direction for dream-alias proposals*:
    `_propose_dream_alias_candidates` now orders from/into by
    degree+fact evidence like the write-dedup detector, instead of filing
    (new, existing) verbatim — 29 proposals in the triage were real
    duplicates offered in the wrong direction, unactionable by accept.
  - *Accepted edges leave the dubious queue — and stay out*:
    `accept_link` floors the edge confidence at 0.7 (above the 0.6
    dubious threshold) and stores the edge as `origin="action"` (a
    confirming action), so neither `dubious_edges` nor the next apply's
    name-based `rescore_edges` (which recomputes every agent edge, e.g.
    related-to back to 0.45) undoes the verdict.
- **Slot-duplicate listings stop flagging deliberate structure.**
  Same-entity lesson/world pairs (aspect siblings: approach vs pitfall vs
  correction) are held to a 0.95 similarity floor instead of 0.80 — near-
  verbatim key-mint drift still lists; ordinary siblings (13 of 13 lesson
  listings in the audit) do not. Identifier-keyed sibling entities
  (`arxiv:X` vs `arxiv:Y`) are never listed: different identifiers are
  different referents by construction (15 of 20 world listings).
- **`memory_graph_review` grows the missing verdict and batch triage.**
  New `relate` action settles a related-not-duplicate pair (file vs the
  concept it implements, phase vs programme) by writing the typed edge and
  dismissing the pair in one call — previously only reachable through the
  console REST API. The five id-verdict actions accept `proposal_ids` for
  batch triage (the 2026-08-05 triage took ~470 single calls);
  JSON-stringified lists from MCP clients are coerced.

### Measured (2026-08-05 — separate-pass events pass all four preregistered gates)
- **The separate-pass chronicle design passes its full preregistered gate
  run** (500 questions, 4 judged arms, tag `ev2-sep-0804`; verdict in
  `evals/results/ev2-separate-pass-verdict.json`). Gate 1, rag control:
  delta 0.000, 0 flips over 500 questions vs the independent
  `aggp1-variants-0803` run. Gate 2, the claims-inertness tripwire this
  design exists to pass: the vanilla hybrid arm reproduces the
  `aggp1-variants-0803` hybrid at delta 0.000 with 0 flips over all 500
  questions — a fresh extraction with the events pass added leaves the
  claims bank verdict-for-verdict identical, eliminating the v7 inline
  design's -0.053 claims tax by construction and by measurement. Gate 3,
  weak-set primary (multi-session + temporal-reasoning, n=266): serving
  events lifts hybrid_ev over the same-run hybrid by +0.056 (p 0.00450,
  20 wins / 5 losses), concentrated where the design aimed —
  temporal-reasoning 0.534 to 0.624, multi-session 0.383 to 0.406.
  Gate 4, strong-set non-inferiority (n=234): delta 0.000 with 0 flips,
  despite events being served on 22 strong-set questions — harmless when
  present, not merely absent. `memory.dream.chronicle` remains
  default-off in this change; flipping the default is a separate,
  human-gated decision.

### Changed (2026-08-04 — extractor sidecar unloads its model when idle)
- **The in-stack extractor sidecar no longer holds its ~7 GB model resident
  while idle.** `ops/docker-compose.yml` now passes llama-server
  `--sleep-idle-seconds` (via a `command:` override; knob
  `PSEUDOLIFE_EXTRACTOR_SLEEP_IDLE_SECONDS`, default 300, `-1` restores
  always-resident): after the idle window the server unloads the model and
  drops to a few hundred MB — on shim installs the sidecar is only the
  *fallback* dreamer, so the weights were the box's largest steady-state
  resident in service of a rare failure path. Wake is transparent:
  `/health` answers 200 while sleeping (healthcheck holds, and doesn't keep
  it awake), and the next completion request blocks until the model
  reloads, comfortably inside `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS`, so an
  unattended dream sweep that falls back mid-run waits instead of failing.
  First fallback dream after a long idle pays the reload (seconds on an
  SSD). Existing installs pick this up with a config-only recreate of the
  extractor container (`docker compose ... up -d --no-deps
  pseudolife-extractor`) — no rebuild. No ladder re-run: the extractor
  binary, model file, prompts, and sampling are all bit-identical; only the
  process's idle lifetime changes, and a reload maps the same weights.
  Compose/Dockerfile arg-list drift is pinned by
  `tests/test_extractor_idle_sleep.py`.

### Fixed (2026-08-04 — boot-window memory balloon: no model load before storage connects)
- **`MemoryService._ensure_init` no longer re-loads the embedding model on
  failed init retries.** While Postgres was in crash-recovery after machine
  boot (~3 minutes), every incoming API/MCP call retried the whole lazy init;
  the embedder was constructed *before* the storage connect, so each retry
  loaded a fresh ~2.4 GB Qwen3-Embedding-0.6B and then failed on the
  connect — 12 loads in the 2026-08-04 incident window ballooned the daemon
  to 21.2 GB RSS (31.5 GB cgroup peak) and nearly OOMed the host, with the
  dead copies stuck in torch module reference cycles. Storage now connects
  first (a down database costs a fast connect error, never a model load) and
  an embedder built by a partially-successful attempt is reused by the next
  attempt. Regression-pinned by `tests/test_init_retry_model_reuse.py`;
  reproduced pre-fix as a +2.35 GB/request RSS staircase against an
  unreachable-DB container.

### Added (2026-08-04 — Console Dreamer card: one-click dreamer model switching)
- **`memory.dream.extractor_model_override`** — a model-only override for
  the primary extractor, applied by `resolve_endpoints` *after*
  env-vs-config ownership resolution, so the dreamer model can be switched
  live without flipping `extractor_source` to `config` and re-owning the
  env-managed endpoint/fallback wiring. Primary only; the fallback
  sidecar's model is never overridden. `None` (default) is inert —
  resolution is byte-identical to before. `build_extractor` now delegates
  its env-vs-config resolution to `resolve_endpoints` (one authority for
  builder and status display — its private copy of the logic is exactly
  where the override initially failed to reach, caught by the watched-RED
  builder tests).
- **Console "Dreamer" hero card** (top of the Console tab) — shows the
  *effective* extractor resolution (primary/fallback endpoint → model,
  probe health, last-dream selection, settings owner; `dream_status` now
  carries `primary_model` / `fallback_model` / `extractor_source` /
  `model_override`) and a one-click model picker (Opus 5 / Sonnet 5 /
  Haiku 4.5 / Fable 5, custom `claude-*` names, or endpoint default)
  writing the override knob. The Claude CLI shim honours `claude-*` names
  per request (2026-08-02), so the switch takes effect on the next dream
  with no restarts; `evals/claude_shim.py` adds `claude-fable-5` to its
  `/v1/models` listing. Follow-up: a launch-default alias (`extractor` /
  `bench`) as the effective primary model is resolved to the endpoint's
  concrete model via its `/v1/models` listing (`primary_model_served` in
  `dream_status`, 300s TTL cache, failure = display degradation only), so
  the card reads `→ claude-opus-5` instead of `→ extractor` on stock
  shim deploys.
- **Installer dreamer-model choice** — `ops/install.ps1` / `ops/install.sh`
  now prompt for the shim's launch model on Claude-shim installs
  (`-Model` / `--model` non-interactively; default and recommendation stay
  `claude-opus-5` per `evals/results/dreamer-choice-verdict.json`) and
  forward it to the autostart scripts. `ops/install-shim-autostart.sh`
  gains `--model` — and now launches `evals/claude_shim.py`: it still
  pointed at the removed `sonnet_shim.py` path, so the systemd unit it
  wrote could never start.
- **Console knob gap-fill** — config fields added since July now have
  Console knobs: `dream.literal_gate`/`literal_gate_scope`,
  `dream.min_relation_confidence`, `dream.relation_quarantine_below`,
  `dream.retype_quarantined_max`, `dream.runs_keep`,
  `bm25.cortex_enabled`, `reranker.skip_margin`, and the
  `lessons.synthesize_in_dream`/`infer_outcomes`/`infer_outcomes_max_signals`
  trio (all live, defaults unchanged). Gated-off capabilities
  (`dream.chronicle`, `dream.known_facts_window`, the agg-recall search
  knobs) deliberately stay out of the registry until their preregistered
  gates pass, pinned by `tests/test_console_knob_gapfill.py`.

### Added (2026-08-04 — separate-pass event extraction: chronicle without the claims tax)
- **The dream's chronicle events now come from their own extractor
  call** (`OpenAICompatExtractor.extract_events`, events-only prompt
  pinned to `evals/prompts/events_pass_v1.txt`) instead of riding the
  claims call — the v7 combined prompt measurably cost claim quality
  (-0.053, p 0.011) and never shipped; with the separate pass the claims
  call runs the shipped v5 prompt byte-identically, so interference is
  zero by construction (and is measured as an exact-zero cross-run
  tripwire in the preregistered gate). An events-pass failure is
  non-fatal by design: claims commit, the cursor advances,
  `events_pass_failed: true` is reported, and the batch's events are not
  retried — an additive enrichment layer must never stall consolidation.
  Event writes reuse the existing gated/journaled chronicle path
  unchanged. `memory.dream.chronicle` still defaults off pending the
  gates (`docs/superpowers/specs/2026-08-04-separate-pass-events-design.md`).

### Added (2026-08-04 — chronicle events: dated occurrences as first-class records (schema v28))
- **`chronicle_events`** — the dream pass can now extract *occurrences*
  ("adopted the kitten on May 13") beside current-state facts, the record
  type Phase 1's controlled negative showed was missing from the bank
  entirely. Bi-temporal by design: `occurred_at` (event time, nullable —
  accepted only as an exact `YYYY-MM-DD` and only when the batch corpus
  actually carries date information, never fabricated) vs `recorded_at`
  (transaction time); undated events keep the source's verbatim
  `occurred_phrase` and sort behind dated rows. Additive-only:
  contradiction handling sets `invalidated_at`, never deletes; exact
  restatements dedup. Event descriptions pass the same literal gate as
  claims; every write journals into the v27 dream-run journal (kind
  `event`), so `memory_dream(action="rollback")` reverts them by exact-id
  delete. Temporally-cued `memory_search` calls serve matching live
  events as an `events` block, oldest first. Extraction ships **off**
  (`memory.dream.chronicle = false`) with the shipped prompt unchanged —
  the events-capable prompt is a new measured artifact
  (`evals/prompts/ku_op_prompt_v7_events.txt`, op-probe variant
  `v7-chronicle-events`) that only ships if the Phase 2 preregistered
  gates pass (op adoption + count-decoy hold, ladder conformance, LME
  weak-type gate; BEAM re-run deferred — see the design doc's 2026-08-04
  amendment).

### Measured (2026-08-04 — aggregation-aware recall Phase 1 fails its gates; defaults unchanged)
- **All four Phase 1 retrieval knobs fail their preregistered gates** on the
  500-question within-run variants run (tag `aggp1-variants-0803`; verdict
  and per-knob compare artifacts in
  `evals/results/agg-recall-phase1-verdict.json`). On the weak types the
  knobs target (multi-session + temporal-reasoning, n=266, paired
  permutation vs the same-run vanilla hybrid): contiguity delta -0.147
  (p 0.00000), timeline -0.011 (p 0.70120), enum rendering -0.071
  (p 0.00030), all-three-combined -0.177 (p 0.00000). Timeline also
  regresses the strong non-inferiority set (-0.038, p 0.00340, 0 wins /
  9 losses). Every knob stays default-off — production behavior is
  unchanged. Validity: the cross-run rag control against `alltypes-0803`
  is exactly zero (0 flips over 500 questions), and all four vanilla arms
  reproduce that independent run exactly despite a fresh extraction. The
  controlled negative supports the design doc's Phase 2 hypothesis: the
  weak-type answer material is never extracted into the bank, so no
  retrieval-side change can surface it — events need to be stored as
  first-class records.
- **`evals/longmemeval_bench.py --variants`** builds five hybrid context
  variants per question from the same live service (vanilla, +contiguity,
  +timeline, +enumerated facts, all three), each answered and judged in
  the same row — one extraction pass instead of four (the design doc's
  bank-reuse assumption was wrong: `dump_bank` persists cortex facts
  only, no band entries; documented in the spec's 2026-08-03 Amendment).
  `answer_and_judge` and `report` handle arbitrary context arms;
  `evals/compare_arms.py` gains `--types` (per-type gate subsets) and
  `--arm-a/--arm-b` (cross-arm within-run pairing).

### Added (2026-08-03 — aggregation-aware recall, Phase 1)
- **Three retrieval-side knobs targeting the cross-session
  aggregation/ordering weakness** the 2026-08-03 BEAM 100K + LongMemEval
  all-types runs exposed (per the preregistration in
  `docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md`;
  all default OFF until their gates pass):
  `memory.search.contiguity_neighbors` — each search hit also surfaces its
  stream-adjacent neighbors (same episode, else same source), placed around
  the hit in (timestamp, seq) order and marked `via: "contiguity"`;
  `memory.search.timeline_channel` — temporally-cued queries get
  lexically-relevant entries injected beside the dense/slot/BM25 channels
  and the memory portion of the result ordered by stream position instead
  of score; and `--fact-render enum` on `evals/longmemeval_bench.py` —
  numbered, dated, one-per-line supersession chains and set members in the
  served fact context. Per-call overrides pin the eval harness's rag
  control arm to vanilla retrieval regardless of config, so the
  preregistered tripwire holds by construction.

### Fixed (2026-08-03 — shim autostart pointed at a nonexistent script)
- **`ops/install-shim-autostart.ps1` registered a logon task that could never
  start the shim**: a scripted rename edit had written literal BEL (0x07)
  bytes into the script path (`evals<BEL>nthropic_shim.py`) and the default
  log path — invisible in consoles, immune to substring greps. The paths now
  read `evals\claude_shim.py` and `~\.pseudolife-mcp\claude-shim.log`, and a
  new guard test bans stray C0 control bytes from every tracked text file so
  this class of corruption cannot land silently again.

### Added (2026-08-02 — LongMemEval beyond the knowledge-update slice)
- **`evals/longmemeval_bench.py` gains `--types`** (comma list or `all`;
  default `knowledge-update` with byte-identical artifact names): the other
  five LongMemEval question types add 422 questions for statistical power
  and LME-500 comparability. Non-KU rows use a generic judge variant that
  drops only the KU-specific update clause (abstention and equivalence
  clauses verbatim); rows without a `question_type` — every pre-extension
  artifact — re-judge byte-identically under the KU prompt. Extended runs
  get a type-slug artifact prefix and a per-type summary breakdown.

### Added (2026-08-02 — switch the dreamer model from the Console)
- **The Claude CLI shim honors a per-request `claude-*` model**, so the
  Console's existing Extractor panel becomes a live dreamer-model switcher:
  set settings source = config, endpoint = the shim, and pick
  `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5` in the model
  field — the next dream uses it, no restarts. Alias model names (the
  compose default `extractor`, `bench`) keep the shim's launch model, so
  existing env-driven deploys are unaffected. Knob suggestions updated
  accordingly.

### Changed (2026-08-02 — literal gate enforces by default; the CLI shim is model-agnostic)
- **`memory.dream.literal_gate` default is now `"enforce"`** — unbacked
  digit literals in dream claims are dropped, not just counted. Decided by
  the post-matcher probe re-runs (`evals/results/gate-firing-normfix-verdict.json`):
  remaining flags are dominated by genuinely unbacked literals (derived
  aggregates, imported world knowledge) at 1.3–1.7% of gateable claims.
  `"log"` restores the old observe-only behavior.
- **`evals/sonnet_shim.py` is now `evals/claude_shim.py`** — it has
  served any Claude model via `--model` since 2026-07-26, and the deployed
  extractor is no longer Sonnet. `ops/install-shim-autostart.ps1` gains a
  `-Model` parameter (default `claude-opus-5`, per the same-harness
  dreamer comparison in `evals/results/dreamer-choice-verdict.json`:
  cortex 0.885 vs 0.821, 5/0 paired), registers the task as
  "Pseudolife Claude Shim", and cleans up the legacy task name.

### Changed (2026-08-02 — literal gate learns the extractors' re-formattings)
- **The literal-faithfulness matcher now normalizes the three
  legitimate-reformatting classes the at-scale firing probe surfaced**
  (`evals/results/gate-firing-verdict.json`: 15 of 17 batch-scope flags
  were normalization gaps): spelled corpus numbers back digit tokens,
  hyphenated ranges/unit compounds gate per digit part, `N+` minimums
  match their base number, and `~`-marked approximations are exempt like
  dates. Fabricated-number detection is unchanged — a range with one
  unbacked endpoint still flags that endpoint.

### Added (2026-08-01 — every dream pass is now an auditable, reversible run (schema v27))
- **Schema v27: `dream_runs` + `dream_run_slots` — each dream pass that
  produces claims records a run row (cursor movement, tallies, lifecycle
  status `running|committed|failed|rolled_back`) and a per-claim
  pre-image journal of what every touched slot held before the write.**
  The journal lives outside the facts supersession chain on purpose:
  superseded-row compaction purges that chain in steady state, so it was
  never durable enough to revert from. `memory_dream(action="runs")` lists
  recent passes; `memory_dream(action="rollback")` reverts the latest
  committed pass by replaying its journal in reverse through the normal
  write paths (supersede-back — nothing is deleted; a reverted insert is
  retired via the new `CortexStore.retire_current`). Rollback keeps
  source traces and does not rewind the dream cursor (design doc:
  `docs/superpowers/specs/2026-08-01-dream-run-journal-design.md`).
  Journal retention is newest-N runs (`memory.dream.runs_keep`, default
  50), pruned during the sweep beside superseded-row compaction.
  `memory_history` gains `as_of` (ISO or epoch) for point-in-time reads of
  a slot's version chain, with the compaction-window limitation documented
  at the tool.

### Added (2026-08-01 — literal-faithfulness gate ships; the verbatim-literals prompt is held on measured grounds)
- **Dream claims now pass a deterministic literal-faithfulness gate**
  (`memory.dream.literal_gate`, default `log`): digit-bearing tokens in a
  claim's value (outside date-like spans, which are exempt by design) must
  appear in the source notes — fabricated numbers/identifiers are counted
  (`log`) or dropped (`enforce`, opt-in). The corpus is the whole pull's
  note union by default (`literal_gate_scope = "batch"`): derived sums and
  cross-note values are measured false-drop classes under per-note gating
  (c2op-count-verdict qid 01493427). Counters surface as
  `literal_flagged` / `literal_dropped` in dream results. `enforce` stays
  opt-in deliberately: across every measured arm the gate never fired
  (the extractors fabricate no gateable literals on the bench corpora),
  so its safety where it *does* fire is unmeasured
  (`evals/results/literal-fidelity-verdict.json`).
- **The KEEP-LITERALS-VERBATIM prompt block (v6) does NOT ship** — the
  pre-registered KU gate failed: cascade -0.090 (p = 0.037), 1 win /
  8 losses against the shipped v5 arm with the rag control at delta 0.000
  exactly, and the digit-gold class it was meant to protect regressed
  0.949 -> 0.872 (n = 39). In 7 of 8 cascade losses a previously-correct
  cortex answer went wrong — general extraction degradation, the count-
  block lesson repeated. `evals/prompts/ku_op_prompt_v6.txt` stays
  committed as a measured, pinned, unshipped variant
  (`test_op_prompt_artifact.py`); the shipped `_SYSTEM_PROMPT` remains
  byte-pinned to v5. Full chain + per-qid mechanism breakdown:
  `evals/results/literal-fidelity-verdict.json`,
  `evals/results/compare-c2v6-literal-pairs.json`.
- **`evals/compare_arms.py`** — committed producer for the
  `compare-*-pairs.json` paired-permutation artifacts (all four arms incl.
  the derived cascade, win/loss qids, seed-deterministic; refuses to
  overwrite). The earlier pairs artifacts had no committed producer.
  `ladder_sweep.py` gains `--literal-gate off|log|enforce` and records the
  gate counters in rung artifacts.

### Changed (2026-08-01 — the extractor op prompt ships: hold reversed on measured grounds)
- **The shipped extraction prompt (`dream.py::_SYSTEM_PROMPT`) now solicits
  claim-level `op` for set membership, paired with the counts-are-never-
  members rule** — the option-B hold (2026-07-31) is reversed by maintainer
  decision on the strength of the count-exclusion gate: cascade exactly at
  the op-less control (delta 0.0, p = 1.0), count-class damage reversed,
  sidecar adoption and ladder-rung gates identical
  (`evals/results/c2op-count-verdict.json`). The prompt is byte-pinned to
  the measured artifact (`evals/prompts/ku_op_prompt_v5.txt`,
  `test_op_prompt_artifact.py`) so what runs is exactly what was measured;
  `op_probe`'s variant constructions re-anchor on the committed op-less
  control file so every historical gate artifact stays byte-identical to
  what its gate ran. Both Sonnet extractor mirrors
  (`sonnet_extractor_v1.md` / `v2.md`) regain the membership block from the
  pre-hold state plus the count-exclusion rule in the same style. Dreams
  can now form set-valued slots from conversation; the aggregate-conversion
  guard remains the apply-time backstop for any `op:"add"` landing on a
  stated total.

## [0.12.0] - 2026-08-01 — set-valued memory, and the measurements that earned it

### Fixed (2026-08-01 — YAML loader ran the surprise gate at 0.3 when the key was omitted)
- **A `config.yaml` with a `memory:` block but no `surprise_threshold` key
  silently ran the store gate at 0.3 instead of the documented (and
  dataclass-default) permissive 0.0** — the loader's hand-rolled fallback
  literal had drifted from the dataclass. The fallback now mirrors the
  default, and a guard test pins every hand-rolled fallback in that block
  against the dataclass so the class of drift stays closed
  (`test_yaml_memory_block_omitted_keys_keep_dataclass_defaults`). Found
  during the 0.12.0 docs currency pass; deployments whose config file
  omits the key will store near-duplicate entries again (the documented
  behavior — the surprise gate remains available by setting the key).

### Added (2026-08-01 — count-exclusion op prompt: the reserve rule measured, and it works)
- **The reserve prompt rule named by the guard verdict — `op` never applies
  to counts/totals/quantities — was built and gated, and it repairs the op
  block's measured KU damage** (`evals/results/c2op-count-verdict.json`).
  The arm (`evals/prompts/ku_op_prompt_v5.txt`, byte-pinned to
  `op_probe.VARIANTS["v5-count-exclusion-claim-example"]`) appends the v0 op
  block plus a COUNTS-ARE-NEVER-MEMBERS rule with a single-claim worked
  example. On the KU-oracle e2e (78 questions, reproducible q8_0 server):
  cascade lands exactly at the op-less control (delta 0.0, p = 1.0) — the
  pre-registered ship condition — while cortex (+0.090) and hybrid (+0.077)
  sit nominally above control (n.s.); against the un-ruled op prompt the
  cascade recovers +0.141 at p = 0.004 over the un-ruled op prompt, cortex
  +0.154 (p = 0.008), hybrid +0.103 (p = 0.019). Count-class damage
  reverses (cortex digit-gold 24 → 32, control 31); 5 of the 7 frozen-total
  questions from the census (`evals/results/c2op-count-census.json`)
  recover their gold as a current scalar, and genuine sets still form
  (75 member facts across 22 banks; one bank holds the recovered count-25
  scalar alongside 9 legitimate to-watch members). Wording was selected by
  targeted 7-question extraction probes (`--qids`, below): v3's standalone
  example object induced multi-object JSON parse retries, v4's integrated
  example was parse-clean but recovered one fewer; v5 keeps the dedicated
  example rendered as a single claim object — 5/7 recovery, zero parse
  failures. **The op prompt block remains held in the shipped extractor**:
  this verdict makes v5 the shipping *candidate*, gated on validating rule
  adoption on the deployed small extractor (qwen-27b adoption does not
  prove the sidecar's) and on an explicit reversal of the option-B hold
  decision. One anomaly recorded for future bank comparisons: every
  op-block prompt variant extracts roughly half the current scalars of the
  op-less prompt (442–512 vs 1003) with no measured e2e cost.
- **`longmemeval_bench.py --qids`** runs a named comma-separated subset of
  questions (targeted extraction / bank forensics; composes with `--tag`),
  and **`evals/analyze_frozen_totals.py`** classifies an op run's losses by
  the frozen-total signature against a control run's banks — the CPU
  forecast that sized this arm before any GPU was spent
  (7 of 21 lost questions rule-recoverable, 0 gain-side questions at risk).

### Changed (2026-07-31 — aggregate conversion guard: `add_member` no longer overwrites a stated total)
- **`CortexStore.add_member` no longer converts a scalar slot to a set when
  the current scalar is a number-led aggregate value** (`_is_aggregate_value`:
  `"32"`, `"27 species"`, `"$1,500"`) — the member is instead parked as a
  contender via the existing `_contend` machinery (audit reason
  `"member_add_blocked_aggregate"`), and the scalar stays canonical. Prior
  behaviour destroyed the stated total on the first spurious membership op,
  which the gate verdict measured as a net-negative KU-oracle effect
  (`evals/results/c2op-gate-verdict.json`, full verdict cited below).
  `resolve(entity, attribute, accept=True)` remains the explicit human path
  to overwrite the total with the accumulated member value; `accept=False`
  leaves the total untouched. Non-aggregate scalars (e.g. `"road bike"`) are
  unaffected and still convert one-way to a set on the first `add_member`,
  as before. **Review fix:** an add whose value already equals the current
  scalar now confirms it (`action="confirmed"`, mirroring `write_fact`'s own
  confirm branch) instead of parking a contender identical to itself. The
  dream claim-apply loop's trace-write + reinforcement-bump block now also
  skips `action="contested"` results, the same way it already skips
  `member_invalid`/`member_capped` — a blocked add never populated the slot,
  so tracing it could silently suppress a later, legitimate scalar claim for
  the same slot and source entry via the `has_trace` guard. The skip is
  action-keyed, not guard-keyed: a plain weaker-tier scalar conflict (a
  dream claim landing "contested" via `write_fact`'s own tier guard, a path
  that predates this feature) also no longer writes a trace or reinforcement
  bump — a contested write never populated the slot either, so the trace it
  used to leave asserted a false provenance link and could suppress a later
  legitimate claim the same way. **Gate result (2026-08-01,
  `evals/results/c2op-guard-verdict.json`):** re-running the KU-oracle e2e
  with the op prompt restored and the guard active came back per-question
  identical to the guard-less op run on every arm (0/78 flips), leaving
  cascade still −0.141 at p = 0.006 vs the op-less control. The guard fired
  correctly (3 banks reshaped; the same-value confirm observed end-to-end)
  but conversion destruction turns out to be the visible wreckage, not the
  causal path: under the op prompt the extractor re-routes later count
  updates into member-adds, so the stated total freezes at its first value
  — a clean-but-stale scalar answers no better than a mangled one. The op
  prompt block therefore stays held per the pre-registered rule; the guard
  ships regardless, as bank hygiene and protection for live
  `memory_set_add` writes. The targeted fix for the measured mechanism is
  the reserve prompt rule (`op` never applies to counts/totals) — since
  built and gated, and it recovers the loss; see the count-exclusion entry
  above.

### Added (2026-07-31 — dream extractor claims carry an `op` for set membership)
- **A dream claim may now carry `"op": "add" | "remove"`** to target the
  set-member model instead of the scalar supersede path — a claim without
  `op` is bit-identical to today (`MemoryService.dream_run`'s claim-apply
  loop, `pseudolife_memory/service.py`). `op:"add"` routes through
  `svc.set_add`; `op:"remove"` through `svc.set_remove` (both own their own
  embedding + persistence, same as every other set-slot entry point).
  `member_invalid`/`member_capped` results are logged (info/warning
  respectively) and the dream continues. A scalar claim (no `op`) landing on
  a slot that already holds current members is dropped and logged at INFO
  (`"dropped scalar claim for set slot %s.%s"`, mirroring the existing
  auto-promote guard) rather than crashing the dream or silently overwriting
  the set. A malformed `op` value (anything but `"add"`/`"remove"`) degrades
  to the scalar path with a warning naming the value — never a hard failure
  mid-dream. The extraction prompt (`_SYSTEM_PROMPT` in `dream.py`, and the
  `evals/prompts/sonnet_extractor_v1.md` / `v2.md` mirrors) gained a short
  "collection membership" instruction block with one add and one remove
  example, phrased to keep `op` off plain value updates (a new job, a moved
  city stay scalar supersedes). `svc.set_add` gained an optional
  `confidence: float = 0.7` keyword (forwarded to `add_member`, which
  already accepted it) so the dream `op:"add"` path threads a claim's
  confidence through exactly like its scalar sibling instead of silently
  discarding it. Fixed a review-caught bug (pre-fix, never deployed): the
  per-slot `has_trace` "already formed this slot" guard is keyed by
  `(slot, source entry)` with no member value, so it must gate scalar claims
  ONLY — applied unconditionally, a SECOND `op:"add"` for the same slot from
  the same source entry (two collection items named in one note) read as
  "already formed" and was silently dropped after the first member landed.
  A `member_invalid`/`member_capped` result now `continue`s before the
  trace-write + reinforcement-bump block (nothing was actually stored, so
  nothing should be traced — combined with the guard above, tracing a
  never-stored member could also mask a later legitimate add). A dropped
  scalar-on-set-slot claim now also increments `tally["dropped_set_slot"]`
  so `dream_run`'s result surfaces the count, not just the log line. Covered
  by seven cases in `tests/test_dream.py` (op-add lands a member, op-remove
  removes one, a no-op claim on a member-holding slot is dropped with the
  store unchanged and `dropped_set_slot` incremented, a malformed op falls
  back to scalar with a warning, two `op:"add"` claims sharing one source
  entry both land, `op:"add"` confidence reaches the stored member, and a
  `member_invalid` result skips the trace write). The full story, two
  corrections deep: (1) the C2 gate's "extractor never adopts op" root
  cause was wrong — the model emitted `op` correctly and `extract()`'s
  parse whitelist silently stripped it (fixed; pinned by
  `test_openai_extractor_carries_op_through_parse`; probes 7/7 adoption,
  `op-probe-q8-fixedparse.json`). (2) With the parse fixed and the block
  restored, the definitive paired gate — whose extraction-variance
  baseline came back per-question identical to the control, so every
  delta is attributable — showed the block's net effect is NEGATIVE on
  KU-oracle (cascade −0.141 at p = 0.006): the model applies `op:"add"`
  to stated-total/aggregate slots and the one-way scalar→set conversion
  destroys the total the answer needs; losses concentrate on set-forming
  questions while non-set questions improve. The block is therefore held
  again — the MCP set tools are the set writers. (The conversion guard was
  subsequently built and gated: it holds the bank shape but does not
  recover the lost totals — see the aggregate-conversion-guard entry
  above.) Evidence:
  `evals/results/c2op-gate-verdict.json`.

### Changed (2026-07-31 — set-valued slots surface as one entry per slot, not one per member)
- **`cortex_search` groups a set-valued slot's current members into a single
  entry** instead of surfacing each member as its own hit: `{"kind": "set",
  "entity", "attribute", "value": "m1; m2 (2 members)", "members": [...],
  "score": <max member score>, "contested": false}`. Grouping runs AFTER the
  BM25 fusion pass (fusion itself stays per-record) and always shows the
  slot's FULL current membership — score-descending for members that
  individually ranked, then any current member that didn't — not just
  whichever members cleared the caller's score floor. `evals/rebuild_contexts.py`'s
  `rebuild_fact_lines` composes the identical value string offline via the
  new shared `pseudolife_memory.memory.cortex.compose_set_value` helper;
  bank fact dicts gain an optional `"kind"` (`"member"` marks one row of a
  set — absent means scalar), so a bank dumped before this change rebuilds
  byte-identically (pinned against a committed fixture,
  `tests/fixtures/rebuild_fact_lines_legacy_bank.json.gz`). `evals/longmemeval_bench.py`'s
  `build_contexts` composes a set entry's line from its already-composed
  `value` and garnishes it with "former members" (removed members, oldest
  first) pulled from the set-shaped `history()`, mirroring the scalar
  "earlier values" idiom; the composition is factored into
  `_compose_fact_line` for offline unit testing
  (`tests/test_longmemeval_bench_fact_lines.py`). `_cortex_record_to_dict`
  now includes `"kind"` on every fact dict (`cortex_lookup`, `cortex_dump`,
  `cortex_search` scalar entries). A set entry also now carries
  `"last_confirmed"`, `"asserted_at"`, and `"age"` (all backed by the same
  anchor — max `tx_time or asserted_at` over current members, the same
  priority `_cortex_record_to_dict` uses for a scalar's `"age"`) so
  `mcp_server.py`'s cortex-first block has real dates to render instead of
  going blank for every set slot. Accepted v1 scope: a set entry carries no
  `"freshness_class"` at all (it renders `evergreen` — the class-resolution
  helpers default an absent class that way, same as any unclassified scalar),
  and the anchor backing `last_confirmed`/`asserted_at`/`age` only advances on
  add/confirm activity — `remove_member` stamps `superseded_at` on the
  removed row but never touches those three fields, so removing a member
  never moves the slot's displayed dates. Fixed a latent crash the new set-entry
  shape would otherwise have caused: `mcp_server.py`'s `memory_search` cortex-first
  block indexed a fact's `"confidence"` directly, which a grouped set entry
  doesn't carry — now `.get`. Fixed a review-caught bug in
  `_cortex_bm25_fuse` itself: it keyed its lexical pool by `record.key`
  (the SLOT identity) rather than the record, so every member of a set
  shared one BM25 score and the lexical-only-injection path could only
  ever inject one member per slot — now keyed by `id(record)`, genuinely
  per-record. `evals/lme_v2_smoke.py`'s `build_contexts_v2` hand-rolled the
  same fact-line loop `longmemeval_bench.build_contexts` used to have
  (mislabeling a set's current members as superseded "earlier values");
  routed through the shared `_compose_fact_line` instead. That helper also
  no longer lists a member as "former" in the removed-members garnish if a
  remove-then-re-add currently has it back in the set. Covered by
  `tests/test_cortex_sets.py` (grouping, score-ordering, unranked-member
  inclusion, no-entry-when-nothing-ranks, mixed scalar+set, last_confirmed),
  `tests/test_cortex_bm25.py::test_rebuild_fact_ranking_matches_service_fusion_set_slot`
  (a deliberately engineered order-divergence scenario, confirmed red
  against the pre-fix keying), `tests/test_lme_v2_smoke_fact_lines.py`,
  `tests/test_longmemeval_bench_fact_lines.py`'s remove-then-re-add cases,
  and `tests/test_cortex_fact_currency.py`'s set-slot case (the pre-existing
  currency guard's fixtures were scalar-only and structurally blind to
  whether a set entry carries a date at all).

### Added (2026-07-31 — MCP tools for set-valued cortex slots)
- **`memory_set_add` / `memory_set_remove`** expose the Task 2–4 member
  model on the MCP surface — add/confirm or retract one member of a
  set-valued `(entity, attribute)` slot (many concurrent values, e.g.
  tags, rather than one canonical NOW value). A scalar already at the slot
  converts to a set one-way the first time `memory_set_add` targets it;
  `memory_fact_set` against a slot already converted to a set now raises an
  actionable error naming these two tools instead of the store's own
  `add_member`/`remove_member` vocabulary. Both are minimal tier, alongside
  `memory_fact_set`. Reads still go through `memory_fact_get`, which now
  returns `{kind: "set", members, removed}` for a set slot and treats
  `members: []` (every member removed) as empty — same as a scalar miss —
  rather than as a found record. Covered by `tests/test_mcp_server.py` and
  the Postgres persistence leg in
  `tests/test_cortex_sets.py::test_set_add_remove_survive_pg_hydration_through_the_service`.

### Added (2026-07-30 — cortex slots can hold a set: member-per-record add/remove lifecycle)
- **A cortex `(entity, attribute)` slot can now hold either one scalar value
  (unchanged) or a set of concurrently-current members** —
  `CortexStore.add_member` / `remove_member` / `members` / `slot_kind`
  (`pseudolife_memory/memory/cortex.py`), the mechanics the rest of this
  branch (the MCP tools, the dream `op` field, one-entry-per-slot serving —
  see the entries above and below) is built on. Reuses the scalar
  supersession spine rather than a parallel storage model: a member is a
  `CortexRecord` with `kind="member"` whose `status` cycles `current` ->
  `removed` exactly like a scalar's `current` -> `superseded`, nothing is
  ever hard-deleted, and every add/remove is logged to the same
  `supersession_log` the scalar path writes to.
  - **Add/confirm, never contest.** Unlike `write_fact`, members have no
    provenance-tier dispute path — there is no such thing as a "contested"
    member. A value that dedups (exact normalised match, or cosine >=
    `MEMBER_DEDUP_COSINE` (0.9) against a current member) confirms the
    existing member (bumps `last_confirmed` and, if higher, confidence)
    instead of duplicating; anything else inserts a new current member, up to
    `MAX_CURRENT_MEMBERS` (100) — beyond the cap, further adds are dropped
    (`"member_capped"`) rather than queued or silently applied. A value that
    normalises to empty is rejected outright (`"member_invalid"`): Postgres
    unique indexes treat NULLs as distinct, so an empty-normalised member row
    would silently bypass the per-slot uniqueness constraint on persistence.
  - **Conversion is one-way in both directions of the story.** A scalar
    already at a slot converts to a set the first time `add_member` targets
    it — the scalar row is superseded (kept as audit history) and
    re-inserted as the slot's first member; there is no path back to scalar
    while any member is current. Once every member has been removed, the
    slot holds no current record of either kind, so `write_fact`'s own guard
    (which checks for CURRENT members only) lets a fresh scalar land there —
    the slot "reverts" to scalar life as a byproduct of removing the last
    member, not a dedicated revert call, and the removed member rows stay as
    audit (`members(..., include_removed=True)`), just no longer reflected in
    `slot_kind`.
  - **The v19 duplicate-healing pass on daemon start is now scoped to
    `kind = 'scalar'`** (`pseudolife_memory/storage/schema.py`,
    `ensure_schema`). The pre-existing healing UPDATE (schema v19) partitions
    `facts` rows by `(entity_norm, attribute_norm)` and demotes all but the
    newest `current` row to `superseded`, run unconditionally on every daemon
    start; without the added `AND kind = 'scalar'` predicate it would
    partition member rows the same way and silently demote all but the
    newest member on a slot to `superseded` on every restart.
    `world_facts`/`lessons` healing and the `facts/contested` pass are
    untouched — neither has a `kind` column or a member concept. TDD-verified
    against the pre-fix loop:
    `tests/test_schema_v26.py::test_ensure_schema_healing_is_kind_aware` seeds
    two current `kind='member'` rows on one slot alongside a genuine
    duplicate current `kind='scalar'` pair on another, confirms one member
    was wrongly demoted pre-fix, and both survive post-fix while the scalar
    duplicate still heals to exactly one current row.
  - `slot.polarity` is accepted and preserved verbatim on an added/converted
    member in v1 — it is NOT interpreted. A negated add does not implicitly
    route to `remove_member`; a caller that means "no longer" calls
    `remove_member` itself.
  - Covered by `tests/test_cortex_sets.py` (add, confirm-by-norm,
    confirm-by-cosine, cap, invalid-value rejection, one-way scalar
    conversion, all-removed reverts to scalar, polarity passthrough) and
    `tests/test_schema_v26.py`.

### Changed (2026-07-30 — schema v26: set-valued cortex slots, columns + index split)
- **`facts.kind` (`scalar` | `member`, default `scalar`) and `facts.value_norm`
  columns added**, and the per-slot current-uniqueness constraint splits by
  kind: `facts_slot_current_scalar_uq` (`entity_norm`, `attribute_norm`) keeps
  the existing one-live-row-per-slot invariant for scalar facts,
  `facts_member_current_uq` (`entity_norm`, `attribute_norm`, `value_norm`)
  allows several members to be concurrently current on the same slot. The
  old unscoped `facts_slot_current_uq` index is dropped. Additive/idempotent
  migration; every existing fact defaults to `kind='scalar'` and continues
  to dedupe exactly as before — internal groundwork for set-valued cortex
  slots, no reader/writer behavior change in this change.

### Added (2026-07-30 — cortex fact retrieval gains an opt-in BM25 lexical channel)
- **`cortex_search` can now fuse a BM25 lexical pool with dense cosine** —
  per-call `bm25=True` or `memory.bm25.cortex_enabled = true` — mirroring
  the turn pool's fusion exactly over each current fact's composed
  `entity — attribute: value` text, with lexical hits gated by the
  normalised `bm25.min_score` rather than the caller's dense floor. It
  ships **off**, and the reason is a measurement, not a mood: the
  pre-registered `_s` A/B (`evals/results/bm25-ab-confirmation.json`,
  same banks rebuilt dense-only vs fused, reproducible q8_0 server, rag
  arm byte-identical across arms as control) found the fusion changed
  56/78 served fact contexts with **zero movement** in cortex accuracy
  (0.1795 both), cascade (0.4231 both) or commit rate (16 → 15), while
  the oracle regression-gate slice paid ~1 question (cortex
  0.6923 → 0.6795). Lexical gaps in fact retrieval are real; answer-level
  failures trace to fact coverage, so the default stays honest.
  Covered by `tests/test_cortex_bm25.py`.
- **`evals/rebuild_contexts.py` is now in ranking lockstep with the
  service** (`--bm25` opt-in flag). The first regression-gate run of the
  channel exposed why: the rebuild had its own dense-only ranking, so the
  gate "passed" code it never executed — the cortex arm came back
  byte-identical to baseline. The fusion now lives in both paths and
  `test_rebuild_fact_ranking_matches_service_fusion` pins them together;
  a fusion change that lands in only one place goes red.

### Changed (2026-07-30 — front door re-based to the end-to-end run; cascade published)
- **The README benchmark table now shows the fresh end-to-end measurement**
  (`ceiling-e2e`: fresh qwen-27b extraction under the v25 backbone, BM25-on
  turn retrieval, reproducible q8_0 serving, 3 byte-identical replicates) —
  rag 0.859 / cortex 0.667 / hybrid 0.833 / **cascade 0.936** — replacing
  the held-fixed `ceiling-v25` table, which stays in the benchmarks guide
  with its narrative re-scoped: the concatenation hybrid's "~10 points over
  naive RAG" does not survive the v25 retrieval upgrade end to end, and is
  marked retired as a headline at that site. The published posture is now
  the **commit-gated cascade**, with its pre-registered full-haystack
  confirmation (cascade 0.462 vs rag 0.346, paired permutation p = 0.011,
  commit precision 0.714) published alongside. All ten evidence artifacts
  are committed in the same change (`ceiling-e2e` jsonl/summary ×3 + agg,
  `casc-q8` jsonl/summary, `casc-q8-confirmation.json`) and pinned by new
  rows in `tests/test_eval_evidence.py`; the honest limits — the vs-hybrid
  margin on `_s` is directional only (p = 0.18), and the 14/78 commit rate
  is the open retrieval workstream — are stated in the guide next to the
  claim.

### Removed (2026-07-30 — dead-code sweep, verified zero production call sites)

Every item below was confirmed unreachable before deletion: a whole-tree grep
per symbol (code, tests, evals, ops, docs, and the console's static JS), on top
of the mechanical pass that surfaced it. Nothing here changes behaviour except
the three REST endpoints, which had no caller in the console or anywhere else.

- **Three unused REST endpoints, `pseudolife_memory/web/routes.py`.**
  `GET /api/health` (a duplicate of the top-level `/health` liveness probe in
  `web/api.py`; the same `_health()` payload still rides inside
  `/api/overview`), `GET /api/tags`, and `GET /api/facts/contenders`. No
  reference in `pseudolife_memory/web/static/`, no test, no ops script — the
  console reads tags via `/api/overview` and contenders via the
  `memory_fact_get` MCP tool. The service methods behind two of them stay
  (`list_tags` backs `/api/overview`; `cortex_contenders` backs
  `mcp_server.py`); only the route registrations are gone.
- **`pseudolife_memory/utils/chunking.py` deleted** — `sliding_window_chunks`
  / `sentence_chunks` / `TextChunk` had no importer anywhere. The chunker the
  reference bank actually uses is `reference_bank._chunk_text`;
  `ReferenceConfig.chunk_size` / `chunk_overlap` feed *that* and are untouched.
- **`ContextBuilder` + `SYSTEM_PROMPT_TEMPLATE` and their private helpers
  removed from `memory/context_builder.py`** (with the `memory/__init__.py`
  re-export that was their only reference). Desktop-app legacy: the MCP server
  returns raw entries and the client builds its own context. `_relative_time`
  survives — `service.py` stamps cortex records with it.
- **Five config dataclasses and their loader wiring, `utils/config.py`** —
  `MemoryBankConfig` (`memory.fast_bank` / `slow_bank`), `TitansConfig`
  (`memory.titans`), `ContrastiveConfig` (`memory.contrastive`),
  `ReflectionConfig` (`memory.reflection`), `ChunkingConfig`
  (`AppConfig.chunking`); plus the scalars `MemoryConfig.memory_engine`,
  `DreamConfig.relation_confidence` (its own comment already said
  `edge_confidence()` superseded it), and `ContextConfig.history_length`.
  **Not a breaking config change:** `_dict_to_dataclass` filters to known
  field names and `load_config` reads each section through an explicit
  `if "x" in raw` gate, so unknown keys have always been ignored rather than
  rejected. Verified by loading a v0.4.x-era `config.yaml` carrying every
  removed section: it parses with no error and the live keys beside them are
  still honoured. `TitansConfig`'s docstring claimed it existed to keep such
  files loading; that was never what made them load.
- **Uncalled methods.** `ContinuumMemorySystem.begin_logical_turn` /
  `end_logical_turn` / `slot_view_for_entries` / `introspection` (`memory/cms.py`);
  `MemoryService.extract_slots_regex` (the gateway dream calls `RegexExtractor`
  directly) and `MemoryService._dream_vocab` (a one-line wrapper over
  `_dream_hints`); `ReferenceBank.delete_document`; `slots.format_slots_for_context`
  and `slots.merge_slots_view` (the latter orphaned by `slot_view_for_entries`
  going); `contradiction._shared_anchor` (a thin wrapper over
  `_shared_anchor_kind`) and the `STATE_TRANSITION_SIM_THRESHOLD` back-compat
  alias, whose three tier-specific floors are what the code reads.
  **Note:** removing the two `*_logical_turn` methods leaves the rest of that
  seam dormant but intact — `last_logical_turn` is still a live column in
  `storage/schema.py` and `retrieve(min_logical_turn=...)` still filters on
  it, so re-adding the two setters re-activates the feature. `cms.py` now says
  so at `_in_logical_turn`.
- **`GraphStore` Protocol removed from `memory/graph_store.py`** — nothing
  annotated against it; the port's real contract is the backend-agnostic suite
  in `tests/test_graph_store.py`, which `PostgresNetworkxGraphStore` (alive,
  constructed in `service.py`) still passes.
- **Write-only attributes.** `EmbeddingPipeline.cache_hits` / `cache_misses`
  (incremented on every cache probe, never read — the counters were not
  reported anywhere), `MemoryService._last_user_query`, and
  `PostgresStorage.capabilities` (`ensure_schema` is still called for its side
  effect; only the unread assignment is gone).
- **Unused imports and residue.** `torch`, `RetrievalResult`, and four config
  classes in `service.py`; `dataclasses.field` in `episodes.py` / `slots.py` /
  `world_cortex.py`; `sys` / `time` / `teacher_extract` / `MIRASBand` across
  four `evals/` scripts; a dead `cols` local in `evals/ladder_sweep.py`; an
  `... if False else None` no-op in `tests/test_lessons_service.py`; and the
  orphaned `evals/lme_v2_check1_client.py`, superseded by
  `evals/lme_v2_check_fixd.py`.

### Added (2026-07-30 — the eval harness reports the commit-gated cascade)
- **`cascade` derived metric across the LongMemEval tooling.** Every judged
  run already answers the `cortex` and `rag` arms, and per-question analysis
  of the `ceiling-e2e` artifacts showed the cortex arm's *commitment* (not
  abstaining) is a strong correctness signal — commit precision 46/46 on the
  oracle slice, 0.76±0.05 on the `_s` haystacks. The commit-gated cascade
  (serve the cortex answer when it commits, fall back to rag on "I don't
  know") beat both naive RAG and the concatenation hybrid in all 8 existing
  runs examined (oracle: 0.936 vs 0.859/0.833; `_s`: 0.428±0.023 vs
  0.321/0.367), so the harness now derives it everywhere: bench `--report`
  summaries, `replicate.py agg`, and `replicate.py compare --arm cascade`.
  Pure post-processing — no new answer calls, nothing persisted per-row, old
  JSONLs report it retroactively. The derivation lives in `replicate.py`
  (import-light) and the bench imports it; covered by
  `tests/test_eval_replicate.py`.

### Fixed (2026-07-30 — a console switch that changed nothing, and the escape hatch it hid)

- **`memory.show_superseded` retired; the real gate is now reachable as
  `memory.hide_superseded`.** The console knob was a no-op in both
  directions: `cms.retrieve` deliberately ignores `show_superseded`
  (hard-filtering on supersession is what made a category query miss the
  only entry naming the category), and the gate it *does* read —
  `hide_superseded` — was never a declared `MemoryConfig` field, so
  `load_config` dropped it from YAML and the console could not set it
  either. The affordance documented in `cms.py` ("set
  `config.hide_superseded = True` to restore the v0.7.2 filter") was
  therefore reachable only by hand-assigning an attribute in Python.
  `hide_superseded: bool = False` is now declared, parsed from
  `memory.hide_superseded`, and registered as the console knob "Hide
  superseded"; the dead field and its knob are gone. Old config files
  carrying `memory.show_superseded` still load — the retired key is
  ignored, which is exactly what it already did.
- **Default retrieval behaviour is unchanged**: superseded entries stay
  visible, downranked ×0.55, in both the dense and BM25 pools. That is
  deliberate — they carry knowledge-update recall (LongMemEval KU) and are
  what lets an answer describe a fact's history. Hiding them is a
  debug/audit switch, now documented as such in
  [`docs/guide/retrieval.md`](docs/guide/retrieval.md) and the defaults
  list in [`docs/guide/configuration.md`](docs/guide/configuration.md).
  `tests/test_superseded_visibility.py` pins the default, the gate in both
  pools, the YAML parse, legacy-key tolerance, and the console-knob-to-gate
  path end to end.

### Changed (2026-07-30 — the ceiling table promoted onto the reproducible stack)

- **The published local-ceiling table (README front door + benchmarks guide)
  now shows `ceiling-v25`** — the 2026-07-29 re-judge of the same contexts on
  stock `llama-server` + `q8_0` (3 byte-identical replicates, std 0.0000) —
  in place of the v2 / TurboQuant figures, which move to a marked
  **Superseded** block in the guide. This is the promotion the 0.11.0 entry
  left pending. It is not a cell swap: the old stack was scoring the
  verbatim-input control arm ~6 points low, so four narrative claims
  measured against that control change with the cells —
  - hybrid's margin over naive RAG: ~14 points → **~10 points**
    (0.7308 vs 0.6282);
  - "the fact spine alone matches RAG's accuracy" → cortex **trails RAG by
    ~4 points** (0.5897 vs 0.6282);
  - cortex's share of RAG's token budget: "under 8%" → **~11%**
    (182.5 / 1637.8 tokens);
  - hybrid's context share: ~64% → **~67%** (1101.8 / 1637.8).
  Two things remain held fixed by the context rebuild and are stated under
  the table: **extraction** (the 2026-07-19 bank dumps) and **raw-turn
  selection** (the pre-v25 retriever picked the turns; BM25 is never
  exercised) — only the cortex fact ranking runs under the v25 backbone.
  The E4B-vs-ceiling sentence is re-scoped to the same-stack comparison it
  always was (0.762 ± 0.027 vs 0.710 ± 0.019, both on the TurboQuant fork);
  no cross-stack claim is made against the promoted 0.7308.
  Evidence rows in `tests/test_eval_evidence.py` are repointed in the same
  change: `ceiling-*` accuracy and token rows now check the `ceiling-v25`
  artifacts, new `ceiling-hist-*` rows pin the retained historical block to
  the v2 artifacts, and the README's token column is pinned for the first
  time — unguarded, its hybrid cell had silently drifted to "~1000" against
  an artifact saying 1043.3.

### Fixed (2026-07-29 — the translated front doors claimed a gate that does not exist)
- **i18n source bumped to v6; all five translations re-synced.** The source
  said "a novelty-gated store drops near-duplicates", which is false at the
  shipped default: `surprise_threshold` is `0.0`
  (`pseudolife_memory/utils/config.py:657`, forced for the daemon build at
  `service.py:573-574`), so the gate never fires and nothing is dropped. The
  equivalent claim was corrected in the English README during the 0.11.0
  currency pass; the translated front doors were deliberately left at v5 that
  cycle, and this closes them out. The clause is removed rather than
  reworded — the surrounding sentence is about *what the agent stores*, and
  the gate is not part of that story at the shipped default.
- **The guard now pins the version a reader sees, not just the one the
  machine sees.** Each translation declares its sync version twice: the
  `<!-- i18n-sync: vN -->` comment, and a human-readable line near the top
  (`synced: v6 (2026-07-29)`, `已同步:v6 …`, `同期バージョン: v6 …`). Only the
  comment was pinned, so bumping it alone left every non-English reader
  looking at "v5" while the guard enforced v6. `tests/test_i18n_readme.py`
  now requires the source's `vN (YYYY-MM-DD)` stamp to appear in each
  translation — matched on the stamp rather than any one language's phrasing.

### Added (2026-07-29 — Dependabot told not to re-break the CPU-only image)

- **`.github/dependabot.yml`, ignore-only: `setuptools >=82` will not be
  proposed again.** The pinned torch 2.12.0 itself requires `setuptools<82`,
  so a lockfile bump past that ceiling makes pip discard the pinned CPU torch
  and pull PyPI's CUDA build — the silent 4 GB regression fixed earlier
  today. CI already rejects such a bump with the conflict named; this stops
  the PR from being re-proposed at all. `open-pull-requests-limit: 0` keeps
  scheduled version updates disabled (they never ran before this file
  existed); security updates continue except where ignored. Lift the rule
  only when bumping torch past its ceiling in the same change.

### Fixed (2026-07-29 — the test suite peaked at ~50 GB and could kill its own run)

- **The suite loads each distinct embedding model once per session: peak
  private commit ~49.5 GB → 5.39 GB, runtime 13:23 → 6:44** (full suite, 1792
  tests, measured 2026-07-29 with the fix and its guard tests in the tree). `warm_service` is module-scoped and every `MemoryService` built its
  own `SentenceTransformer`; under the schema-v25 default
  (`Qwen/Qwen3-Embedding-0.6B`, measured **2.52 GB resident apiece**, ~26×
  the MiniLM it replaced) that was ~90 loads per run — enough to crash the
  suite outright on a 64 GB host, which it did once on 2026-07-29. Dropping a
  service frees nothing (reference cycles), and even a full `gc.collect()`
  returned only a quarter of the memory (the allocator keeps its arenas), so
  the footprint was a one-way ratchet.
- `tests/conftest.py` now memoizes the **model load** — deliberately not the
  `EmbeddingPipeline`, so every pipeline keeps its own encode LRU and dim
  state and nothing crosses a test boundary. One piece of state that separate
  instances provided implicitly survives by design:
  `EmbeddingPipeline.__init__` caps `model.max_seq_length` *in place*,
  reading the current value as its floor, so a shared model would ratchet the
  cap down permanently across configs. The memoization key therefore includes
  the config's cap; differently-capped pipelines get their own model. Pinned
  by `tests/test_shared_embedding_weights.py`, whose hazard test was verified
  load-bearing (removing the cap from the key turns it red). Production is
  untouched — the daemon builds one service per process.

### Fixed (2026-07-29 — the CPU-only image had been shipping CUDA for five days)

- **The daemon image is CPU-only again: 12.6 GB → 5.03 GB, a 7.57 GB (60%)
  saving.** Every image built between 2026-07-24 and 2026-07-29 shipped
  `torch 2.13.0+cu130` and 19 `nvidia-*` / `cuda-*` / `triton` distributions —
  a full CUDA runtime inside a service that sets `CUDA_VISIBLE_DEVICES=-1`
  and has no GPU code path. Verified on the deployed build: the container now
  reports `torch 2.12.0+cpu`, `torch.version.cuda = None`, and zero CUDA
  distributions.

  **Root cause — a dependency *ceiling*, not a raised floor.** Nothing in the
  graph requires torch newer than the pinned 2.12.0 (the highest declared
  floors are extras-only, at `>=2.2`/`>=2.4`). The trigger ran the other way:
  `torch 2.12.0` itself requires `setuptools<82`, and a Dependabot bump
  (`17b97180`, 2026-07-24) moved `ops/requirements.lock.txt` to
  `setuptools==83.0.0`. A pip constraint file cannot be negotiated, so pip
  reported `Requirement already satisfied: torch>=2.1.0 ... (2.12.0+cpu)`,
  then backtracked *off* the pinned CPU torch — discarding it rather than the
  setuptools pin — and walked forward to 2.13.0, which declares no setuptools
  ceiling. PyPI's default linux torch wheel is the CUDA build, so ~4 GB of
  CUDA arrived with it. The constraint that poisoned the resolution never even
  reached the image: the Dockerfile forces setuptools back to 78.1.1 on the
  next line.

- **`ops/requirements.lock.txt` now pins `torch==2.12.0+cpu`** and holds
  `setuptools` at 78.1.1 (below torch's ceiling, and the CVE-2025-47273 patched
  release the image already shipped). torch used to be deliberately *absent*
  from the lock on the theory that installing it first made it unnecessary —
  which left the single largest dependency in the image unpinned. Because the
  `+cpu` local version exists only on PyTorch's own index, a future dependency
  that genuinely needs a newer torch now fails the build loudly
  (`ResolutionImpossible`, naming the conflict) instead of quietly swapping in
  a CUDA wheel.

- **The `ops/Dockerfile.daemon` comment asserting the opposite is corrected.**
  It claimed the up-front CPU install meant the constrained install "never
  pulls the multi-GB CUDA build from PyPI" — false for the whole window above.

- **Guard added** (`tests/test_daemon_image_is_cpu_only.py`): the lockfile must
  pin a `+cpu` torch and carry no CUDA distributions; the Dockerfile, the
  lockfile, and CI must not drift apart on the torch version; and the lockfile's
  `setuptools` pin must satisfy the pinned torch's own ceiling — the last of
  which fails on exactly the Dependabot bump that caused this. A `slow`-marked
  test asserts it end-to-end against the built image, skipping when Docker or
  the image is absent.

### Added (2026-07-29 — supersede-at-discovery affordances)
- **Aged, stale, or contested facts now carry a ready-made `correct_with`
  call on the MCP read surface.** `memory_search` (cortex block),
  `memory_fact_get`, and `memory_world_search` attach the exact
  `memory_fact_set(...)` / `memory_world_set(...)` invocation for the
  fact's slot, and the response carries a one-line `correction_note`
  stating the norm: if the fact contradicts observed reality, run the
  correction at discovery — re-assert the same value to confirm it.
  Motivation: a session recalled a world fact describing work as pending
  that had shipped 11 days earlier, reported the contradiction in prose,
  and left the record standing (2026-07-29); the briefing instruction
  alone demonstrably does not produce supersede-at-discovery, so the
  affordance moves to the moment of recall, where correcting costs a
  copy-paste instead of a recalled procedure.
- **`freshness.needs_correction_nudge`** gates the affordance at **TTL/3**
  (volatile ≈ 7 days, slow ≈ 90 days, evergreen never) — deliberately
  ahead of the 2×TTL `stale` flag, which the 11-day-old incident fact had
  not yet reached. Stale and contested facts are flagged regardless of
  age. Pinned by `tests/test_correction_affordance.py`. The tool
  docstrings deliberately do not describe the field — the core manifest
  budget is at capacity, and the in-band `correction_note` teaches it at
  the moment it applies.
- **TRUST ORDER briefing sharpened** (both `session_hook.py` and its
  byte-pinned twin `examples/CLAUDE.memory.md`): recall results mark
  aged/contested facts with `correct_with`; correcting is part of
  discovering, not a follow-up. Session-end reflection (detecting
  recalled-but-contradicted facts at episode close) was evaluated and
  deferred — the observed failure mode stores nothing for the daemon to
  detect; see `docs/superpowers/specs/2026-07-29-supersede-at-discovery-design.md`.

## [0.11.0] - 2026-07-29 — a better retriever, and facts that admit their age

The embedding backbone moved to `Qwen/Qwen3-Embedding-0.6B` at 1024
dimensions (schema **v25**), chosen by a recall shootout on this project's
own corpus rather than on a leaderboard: **R@10 0.572 → 0.809** over a
74,183-turn haystack, with queries and documents now encoded asymmetrically.
Alongside it, canonical facts stopped pretending to be timeless — every
cortex fact carries `asserted_at` and a human `age`, and a `freshness_class`
(schema v23) decays confidence and flags `stale`, inferred by default from
the entity's kind (schema v24). A round of band-integrity fixes closes paths
where overflow could destroy memories outright, and four sources of
graph-review noise were closed at the write path instead of swept up after.

**Upgrading requires one manual step.** The `vector(384)` → `vector(1024)`
move is not additive: the daemon **refuses to start** against an
older-dimensioned bank rather than half-migrating it. Back up, stop the
daemon, and re-embed offline with `ops/migrate_embeddings.py` — see
[the v25 migration runbook](docs/runbooks/embedding-v25-migration.md).

### Fixed (2026-07-29 — `mcp 2.0.0` broke every fresh install the day it shipped)
- **The `mcp` dependency is upper-bounded to `<2`.** `pyproject.toml` asked
  for `mcp>=1.0.0` with no ceiling, so the moment the SDK published 2.0.0 a
  clean `pip install` resolved to it — and 2.0.0 relocated
  `mcp.server.fastmcp`, which `pseudolife_memory/mcp_server.py` imports at
  module scope. The failure is at import, so it takes out the server, not
  merely a test. `ops/requirements.lock.txt` has always pinned a 1.x
  (currently `1.28.1`, verified importing `fastmcp` in the running
  container); this makes the source distribution agree with the image
  instead of quietly resolving somewhere else.
  **Why it surfaced here:** nothing in this release touched dependencies.
  Master's last CI run was green five hours earlier on the same tree — the
  release branch simply had the misfortune to be the first build after the
  upstream major. A local venv pinned at `1.27.2` could never have caught
  it; only an unpinned resolve does, which is exactly what a user gets.
  Supporting 2.x is a real migration (the SDK is used across `fastmcp`,
  `lowlevel`, `client.session`, `streamable_http` and `stdio_server`), not a
  version bump, and is deliberately not attempted here.

### Added (2026-07-29 — the old serving stack was biased, not just noisy)
- **The ceiling table re-measured on the reproducible server, and the control
  arm moved further than either treatment arm.** `ceiling-v25` rebuilds the
  published ceiling run's contexts from the same committed bank dumps under
  current v25 knobs and re-judges on stock `llama-server` + `q8_0`
  (`evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-v25.agg.json`,
  3 replicates, std 0.0000 on every arm):

  | arm | v25 / q8_0 | published (v2 / TurboQuant) | Δ |
  |---|---|---|---|
  | naive RAG (**control**) | 0.6282 | 0.5667 ± 0.0167 | **+0.0615** |
  | cortex facts only | 0.5897 | 0.5590 ± 0.0295 | +0.0307 |
  | hybrid | 0.7308 | 0.7102 ± 0.0194 | +0.0206 |

  `rebuild_contexts.py` copies the rag context **verbatim** — that arm's
  input is byte-identical across the two runs — so its +0.0615 cannot be a
  retrieval effect. It is the answerer/judge stack alone, and at 3.7× the old
  measurement's own standard deviation it is a **systematic offset, not
  variance**. That sharpens the 2026-07-27 finding: the TurboQuant fork's
  fused TBQ4_0 KV was not merely flipping ~7% of verdicts at random, it was
  scoring this slice about six points low.
  All three replicates came back **byte-identical** (`sha256` prefix
  `1a97bea2caa3e191d5ab7687c73546c9`) — not merely equal in score but
  identical answer for answer and verdict for verdict, which is the strongest
  available statement that the reproducible config is reproducible. Only the
  base `.jsonl` is committed for that reason: the other two replicates are
  the same bytes, and the aggregate already records all three accuracies.
  **Consequence:** no number measured on the old stack is comparable to one
  measured now, in either direction, and the gap is larger than most of the
  effects these tables report. The deltas above are therefore *not* evidence
  about v25 — the clean same-stack v25 comparison is the regression gate
  below. The published tables are left as they stand pending a deliberate
  promotion; `ceiling-v25` is committed as the tagged candidate.
  *[Superseded 2026-07-30: the promotion has landed — the README and guide
  tables now publish `ceiling-v25`; see Unreleased.]*

### Fixed (2026-07-29 — a sixth file was coupled to the "five-file version cut")
- **`tests/test_ops_update_rollback.py` derives the daemon version from
  `ops/docker-compose.yml` instead of hard-coding it.** The assertion pinned
  the literal `pseudolife-daemon:0.10.0-unittest`, so it went red the moment
  this release bumped the compose tag — the version cut presenting itself as
  a regression in the rollback path, which is the one path you least want to
  distrust while cutting a release. The update scripts already read that tag
  from compose as the single source of truth; the test now reads it the same
  way, and no longer needs touching on a cut.

### Added (2026-07-29 — the v25 backbone finally ran the regression gate)
- **The gate PASSES on the shipped v25 configuration**, measured against the
  pinned oracle/`e4b-ft` arm1 slice and committed as
  `evals/results/regression_gate-2026-07-29-v25-backbone-verify.agg.json`
  (2 replicates, 78 questions, contexts rebuilt from banks with current
  knobs). Deltas against the `1f0f13a` baseline:

  | arm | v25 | baseline | Δ |
  |---|---|---|---|
  | naive RAG (control) | 0.6282 | 0.6282 | 0.0000 |
  | cortex facts only | 0.6923 | 0.7051 | −0.0128 |
  | hybrid | 0.7821 | 0.7692 | +0.0129 |

  Both moved arms sit inside the 0.03 margin, and `std` is 0.0000 on all
  three across both replicates — confirming the run was served by the
  reproducible q8_0 config and not the fast build. The control arm is
  *byte-identical*: it reads raw turns and never touches the retriever, so
  its zero is what licenses reading the other two as real.
  **Why this is a late entry:** the baseline was established 2026-07-27
  (`1f0f13a`) and the backbone swap landed 2026-07-28 (`7d20443b`), so the
  single most retrieval-affecting change in this release reached the release
  gate without the retrieval gate ever having been run against it. On the
  slice, a 1024-d backbone with R@10 0.809 buys nothing end-to-end over a
  384-d one at 0.572 — better recall did not become better answers here,
  which is worth knowing before reading the shootout as a product claim.

### Fixed (2026-07-29 — the `relate` action suggested a relation no fresh bank could store)
- **`implements` is now a builtin relation.** The file/concept review finding
  added on 2026-07-26 proposes `<file> implements <concept>`, but `implements`
  was never in `_BUILTIN_RELATIONS`, and the relation registry is a closed
  vocabulary: `graph_relate` rejected it with `unknown_relation` and — because
  `implements` matches no seeded name above `difflib`'s 0.5 cutoff — an
  **empty** `suggestions` list, leaving the user no recovery hint. Seeding is
  idempotent (`ON CONFLICT (name) DO NOTHING`), so existing banks pick it up
  on the next daemon start.
  **Why it survived review:** the relation had been hand-defined on the
  development bank, so every manual test of the Atlas Relate button passed.
  The feature was broken only on a *fresh* install — the one configuration
  nobody had. That CHANGELOG entry even asserts "the `implements` relation
  already described exactly this case", which was true of the live bank and
  false of the shipped vocabulary. The regression test now resolves every
  relation the review layer can suggest against `_BUILTIN_RELATIONS` rather
  than against whatever the live registry has drifted to.

### Added (2026-07-29 — the Arm-1 significance claim gets its artifact)
- **`replicate.py compare` run for the Arm-1 vs baseline pair**, producing
  `longmemeval-ku-oracle-e4b-ft-arm1-vs-baseline-{cortex,hybrid,rag}.compare.json`.
  The published p-values reproduce exactly (cortex Δ+0.0795 p=0.16958; hybrid
  Δ+0.0128 p=0.82862), so the table was accurate — it simply had no evidence
  behind it, which is the failure `tests/test_eval_evidence.py` exists to
  prevent. The **`rag` control arm** (Δ−0.0103, p=0.40586) is now published
  beside them: its input is identical across both runs, so it bounds what the
  other arms can claim.

### Changed (2026-07-27 — the gate's spread was the serving stack, not the judge)
- **`regression_gate.ps1` defaults to `-Replicates 2` again, and the baseline
  is re-established at n=7 with std 0.0000 on every arm** (rag 0.6282,
  cortex 0.7051, hybrid 0.7692). The ~0.03 cortex spread that justified
  raising the default to 10 the previous day was the TurboQuant fork's fused
  TBQ4_0 KV cache, not judge nondeterminism: it flips ~6.8–7.7% of verdicts
  on byte-identical input (`evals/results/judge-determinism-check.json`),
  while the stock `llama-server` with `--cache-type-k/v q8_0` reproduces
  exactly. Replicates are therefore a drift canary, not an estimator — if two
  disagree, the run was served by the wrong binary. The gate also got *more*
  sensitive: the margin `max(0.03, 2*std)` collapses to the 0.03 floor
  instead of the 0.0637 the noise used to buy.
  **Why this entry is dated late:** the change shipped on 2026-07-27 and
  `evals/README.md` retired the superseded numbers at their own site, but the
  CHANGELOG was never updated, so `[Unreleased]` went on presenting the
  10-replicate baseline as current for two days.

### Added (2026-07-28 — Docker build-cache retention)
- **`ops/prune-build-cache.ps1` / `.sh`** give the BuildKit cache a
  retention policy: an age pass (`docker builder prune --force --filter
  until=168h`, `-MaxAgeHours` / `--max-age-hours`), then a size-ceiling
  pass (`--max-used-space`, default 20GB, `-MaxUsedSpaceGB` /
  `--max-used-space-gb`) only if still over cap after the age pass, then a
  Windows/WSL-only `fstrim` of the `docker-desktop` disk. `-DryRun` /
  `--dry-run` reports an estimate and changes nothing; `-NoTrim` /
  `--no-trim` runs the prune but skips fstrim. The sibling of the
  2026-07-14 rollback-tag retention: one deploy produces ~12.45GB of cache
  across 17 entries, and 51.87GB across 169 entries — all inactive, some
  5-6 weeks old — had accumulated by 2026-07-28, with ~52GB regrowing in
  the 13 days after the prior manual trim.
- **fstrim is invoked as `wsl -d docker-desktop -e sh -c "fstrim -v
  /mnt/docker-desktop-disk"`, not as the bare `-e fstrim` target.** `wsl -d
  <distro> -e <cmd>` fails to resolve `fstrim` when passed directly as the
  `-e` target, even though `/sbin` (where `fstrim` lives) is on the
  child's `PATH` (`execvpe(fstrim) failed: No such file or directory`);
  `sh -c` works because the shell performs its own `PATH` lookup instead
  of relying on wsl's relay to do it. Verified live to reclaim 207.2MiB.
  Pinned by `tests/test_ops_prune_build_cache.py`.
- **The size-ceiling pass is a weaker backstop than the age pass, not a
  peer mechanism.** Measured 2026-07-28 against a 12.45GB / 17-entry
  cache: `docker builder prune --force --max-used-space 8000000000`
  reclaimed **0B**, because 14 of the 17 entries were `Shared=true` with
  the live daemon image, and a build-cache prune cannot free layers an
  existing image still holds — only the 3 unshared entries (3.314MB
  total) were actually prunable. The ceiling pass can therefore be a
  no-op during exactly the "heavy build week" it exists to cover; it only
  starts reclaiming once that cache has unshared, i.e. after the images
  holding it are removed (e.g. by `ops/prune-rollbacks.*`). The age pass
  is the primary mechanism in the steady state.
- **`ops/update.ps1` / `.sh` prune after a healthy deploy**, via
  `-KeepCacheHours` / `--keep-cache-hours` (default 168) and
  `-NoCachePrune` / `--no-cache-prune`. Placement is load-bearing: before
  the build it would delete the cache that build reuses, and on the
  unhealthy path it would strip the cache a rollback rebuild needs (that
  branch exits first, so retention is skipped for free). Non-fatal —
  retention never fails a deploy that already succeeded.
- **`ops/install-cache-retention.ps1`** registers a weekly Windows
  Scheduled Task (`-DayOfWeek` / `-At`, default Sunday 03:00;
  `-Unregister` removes it) for stretches with no deploys. Windows-only,
  and must be registered from the permanent checkout, not a worktree — it
  bakes its own directory into the registered command, so a worktree
  registration breaks silently once that worktree is deleted. Re-run it
  if the checkout ever moves.
- **`ops/compact-docker-vhdx.ps1` + `docs/runbooks/docker-disk-retention.md`.**
  `fstrim` frees space inside the VM but never shrinks the host `.vhdx`;
  only an offline `Optimize-VHD -Mode Full` does, and that needs
  elevation plus full Docker downtime, so it stays manual. The script
  pre-flights `Optimize-VHD` availability (ships with the Hyper-V module;
  absent on Windows Home) and rejects a non-file `-Path` before stopping
  anything.
- Scope guard: these scripts only ever call `docker builder prune`. Never
  images, containers, `docker system prune`, or any volume command —
  enforced by `tests/test_ops_prune_build_cache.py`, not by convention.

### Fixed (2026-07-28 — five `ops/*.sh` scripts were not executable in git)
- **Executable bit restored on `ops/install-shim-autostart.sh`, `ops/install.sh`,
  `ops/preflight.sh`, `ops/prune-build-cache.sh`, `ops/prune-rollbacks.sh`** —
  all five were tracked as mode `100644`, which clones as non-executable on
  Linux/macOS. `ops/update.sh` invokes `prune-rollbacks.sh` and
  `prune-build-cache.sh` by bare path; both failures were swallowed by a
  `WARNING`-and-continue wrapper, so rollback-tag retention has been a silent
  no-op on every Linux/macOS deploy since `prune-rollbacks.sh` shipped
  (commit `b47430de`, 2026-07-14). `ops/install.sh` being non-executable also
  broke the documented quickstart (README.md and every translated
  `docs/i18n/README.*.md` tell a fresh clone to run it directly).
  `tests/test_ops_script_modes.py` guards the git index mode of every tracked
  `ops/*.sh` file so this cannot regress silently again.

### Added (2026-07-28 — v25 embedding migration script)
- **`ops/migrate_embeddings.py`** takes a live v24 bank (four
  `vector(384)` embedding columns) to v25 (`vector(1024)`) offline: dry-
  run by default (writes nothing without `--apply`), requires
  `--backup-verified` to apply, and refuses to apply while the daemon
  answers its health endpoint (a live writer re-embedding underneath the
  daemon's own in-memory state would corrupt the bank). Each of
  `entries`/`facts`/`world_facts`/`lessons` is migrated in its own
  transaction — drop NOT NULL where present, `ALTER COLUMN embedding TYPE
  vector(1024) USING NULL`, re-embed every row through the real
  `EmbeddingPipeline` and the exact claim-text shape the write paths use
  (`cortex_write`/`world_write`/`lesson_write`'s
  `f"{entity} {attribute} {value}".strip()`; entries re-embed their
  stored `text` verbatim), restore constraints — and creates no index
  (there is none to rebuild; `ensure_schema` already drops
  `entries_embedding_idx` unconditionally on every boot). Stamps
  `meta.schema_version = 25` last, only after all four tables migrate
  without error. This is the human-gated remedy for schema v25's
  additive-only `ensure_schema` refusing to start against a v24-
  dimensioned bank; running it against the live bank is a user-gated
  morning step, not part of this change.

### Changed (2026-07-28 — embedding backbone swap; schema **v25**)
- **Default embedding model is now `Qwen/Qwen3-Embedding-0.6B`** (was
  `all-MiniLM-L6-v2`). Measured on the project's own LongMemEval-derived
  corpus (150 questions, 74,183 haystack turns, 299 gold; PR #59
  artifacts): R@10 0.809 vs bge-base-en-v1.5's 0.742 and shipped MiniLM's
  0.572 (+81/-6 @5 vs MiniLM, p≈0). fp32 torch in-process, no GPU
  sidecar: 2.4 GB RAM, ~82ms median / ~101ms p90 per query on CPU with
  the project's existing sentence-transformers/transformers versions —
  no dependency bump. Qwen3-Embedding is instruction-asymmetric
  (`EmbeddingConfig.query_prefix`, `EmbeddingPipeline.encode_query` —
  landed ahead of this swap); every retrieval probe across
  `service.py`'s 23 classified encode sites now goes through
  `encode_query` (see the threading entry below).
- **Schema v25: `entries`/`facts`/`world_facts`/`lessons.embedding` move
  from `vector(384)` to `vector(1024)`.** `ensure_schema` stays
  additive-only — a vector dimension change is not additive, so it now
  REFUSES to start (before any DDL, naming `ops/migrate_embeddings.py`)
  when a live bank's `entries.embedding` is dimensioned but not at 1024,
  rather than half-migrating four tables at startup or writing
  1024-d vectors into 384-d columns. The ONNX backend has no in-repo
  export for Qwen3-0.6B, so the daemon runs the torch backend for it
  (falls back cleanly, same fail-soft path as any other ONNX-unavailable
  model); the ONNX machinery itself is unchanged and still used for
  MiniLM-family models.
- Threshold defaults calibrated on MiniLM cosine distributions
  (`alias_candidate_min_cosine`, `curation_min_similarity`, surprise
  gate, recall `min_score` floors) are left unchanged — those absolute
  cosine distributions shift under a new backbone, but recalibrating
  them is out of scope for this change and is deferred to live data.
- **Retrieval floors (the `min_score` 0.2/0.25 class) now gate a
  prefixed-query-to-document cosine, not a doc-to-doc one** — now that
  query-side call sites use `encode_query`'s instruction prefix
  (Task 3), those thresholds' semantics shifted, not just their scale;
  left unrecalibrated pending live data, per the bullet above.
  `supersede()`'s embedding-fallback paraphrase probe is one of the
  now-asymmetric comparisons, so it reads as somewhat more conservative
  at the shipped default.

### Fixed (2026-07-28 — v25 review fix wave)
- **The daemon image now bakes `Qwen/Qwen3-Embedding-0.6B`** — it
  previously baked only `all-MiniLM-L6-v2` while the default moved to
  Qwen3 under `HF_HUB_OFFLINE=1`, so a container built from that state
  booted healthy and then threw `OSError` on the first memory tool call.
  A guard test pins the Dockerfile bake to `EmbeddingConfig.model_name`'s
  default so the two can't drift apart again.
- **Legacy `.pt` bank migration re-embeds on a dimension mismatch.**
  `migrate_legacy` used to insert a legacy bank's stored entry embeddings
  verbatim; a real legacy bank is 384-d (MiniLM-era) and now fails a
  `vector(1024)` insert every boot (swallowed to a retried warning). It
  now re-embeds any entry whose stored embedding doesn't match the live
  pipeline's dimension, through that same `EmbeddingPipeline` instance.
- **`migrate_legacy`'s cortex-facts branch gets the same re-embed-on-
  dim-mismatch treatment.** It previously inserted a legacy fact's stored
  claim embedding verbatim via `replace_facts` — on a real (384-d) legacy
  bank this raised AFTER the entries loop had already committed, so the
  `.pre-v8.bak` rename was never reached and the idempotency guard
  (`storage.load_entries() or storage.load_facts()`) then permanently
  blocked every retry, losing the legacy facts for good. Now re-embeds
  from each record's own `(entity, attribute, value)` claim text before
  insertion, same as the entries branch.
- **`ops/restore_from_pt.py` refuses on an embedding-dimension mismatch**
  instead of inserting a snapshot's vectors verbatim. It restores a
  same-era `.pt` snapshot into the live bank (disaster recovery, not a
  migration tool), so a pre-v25 384-d `.bak` restored after the live bank
  moved to `vector(1024)` previously either raised partway through the
  entries loop (a partial, silent restore — each `insert_entry` commits
  on its own) or risked corrupting a future batched insert path. It now
  checks every stored embedding's length against the live bank's declared
  dimension before writing anything, and exits 1 with nothing written on
  any mismatch.
- **`/health` reports `init_refusal` + `status: "degraded"`** when
  `MemoryService._ensure_init`'s storage construction hits schema v25's
  dimension-mismatch refusal — previously invisible until the first tool
  call, since the refusal fires lazily and `/health` doesn't construct
  storage eagerly.

### Added (2026-07-28 — embedding backbone decided on our corpus: Qwen3-Embedding-0.6B)
- **Seven-arm fp32 shootout** on the LongMemEval slice (150 questions,
  74,183 haystack turns, 299 gold;
  `evals/results/embedder-recall-shootout-20260727.json`):
  Qwen3-Embedding-0.6B reaches R@10 **0.809** vs bge-base-en-v1.5 0.742,
  beating every arm at every k. Both anchors (MiniLM, bge-base) reproduced
  the PR #44 artifact exactly. bge's card-recommended query prefix does not
  help on this corpus; granite-r2 and arctic-l-v2 land below bge-base
  despite higher leaderboard standings — leaderboard rank did not transfer,
  twice.
- **Direct paired test** (`embedder-recall-qwen-vs-bge-20260728.json`):
  Qwen3 over bge-base +32/−12 at k=10, p=0.004; significant at every k.
- **Quantization round** (`embedder-recall-quant-shootout-20260728.json`):
  Q8_0 GGUF matches fp32 (R@10 0.806 vs 0.809, statistical noise) at a
  quarter of the RAM (0.6 GB). Scale does not pay at Q4: the 4B at Q4_K_M
  lands BELOW the fp32 0.6B (R@10 0.753, the round's only significant
  delta — negative), and 8B-Q4 plus both Nemotron-3-Embed-1B forms are
  statistical washes against the 0.6B at 4–8x its footprint. Matryoshka
  truncation to 1024d measured free on both 4B and Nemotron.
- Harness (`evals/embedder_recall.py`): candidate registry with
  card-verbatim prefixes recorded per arm (instruction-tuned embedders
  swing on exact wording), llama-server GGUF adapter (forced
  `--pooling last`; cross-validated against fp32 at cosine 0.9987 before
  any arm ran), Matryoshka-truncation arms, per-gold hit vectors persisted
  for post-hoc pairwise tests, incremental artifact writes, 512-token cap
  on every arm for fairness and VRAM safety.
- **The backbone swap itself (schema v25, `vector(1024)`, full re-embed)
  has NOT been performed** — this entry records the model decision and its
  evidence only.

### Fixed (2026-07-27 — user-set entity kinds survive a model re-apply)
- **`apply_entity_kinds` now locks `origin='user'` rows against the
  artifact.** The classifier repeats its mistakes — the `miras-bands`
  mislabel appeared in both gold replicates — so the first hand-correction
  written to `entity_kinds` would have been silently reinstated by the next
  re-apply, and the resulting `evergreen -> volatile` flip would not even
  show in the downgrade section, because it looks like the normal
  direction. User rows now win over a disagreeing artifact (reported in the
  dry run), and are skipped on agreement too — re-upserting would churn
  `origin` back to `model` and unlock the row for the run after.

### Added (2026-07-27 — freshness is inferred from the entity's kind; schema **v24**)
- **`entity_kinds` (schema v24) stores one kind per entity** — `artifact`
  (frozen in time), `system` (live), `concept` (abstract) — and
  `freshness.resolve_class(kind, attribute)` turns that into a fact's
  `freshness_class`. Only `system` entities can yield `volatile`, so the 282
  facts about frozen artifacts are structurally protected from decaying.
- **Why kind and not attribute name.** `0-9-0-release / schema-version` is
  permanently true; `daemon / schema-version` rots. Same attribute, opposite
  class — the name cannot decide it, the entity's kind can.
- **No model call on the write path.** A new fact looks its entity's kind up in
  a cached map and applies a pure function, because this runs on every dream.
  `freshness_class` now defaults to the sentinel `"auto"`; explicit values are
  still honoured, and an empty kind map reproduces v23 behaviour exactly.
- **Scoping, not batch size, is the token lever.** An entity only matters if it
  carries a transient attribute: 2,423 fact pairs → 265 scoped → 33
  rule-confident → 232 needing model judgement, a 10.4× reduction before a
  single call (measured 2026-07-27 on the live bank; these counts drift as
  the bank grows — reproduce with `python evals/classify_entity_kinds.py
  --scope-only`). Backfill runs at batch 50 over five calls. The backfill has
  **not** been run against the live bank — this ships the machinery only; an
  empty `entity_kinds` table resolves every fact to `evergreen`, so behaviour
  is unchanged until it is.
- Measured first: entity *aliasing* was the presumed root cause and is
  explicitly out of scope — on the live bank only ~4–6 of 74 lexical clusters
  are genuine aliases, reproducing the Stage 1.5 finding on real data.

### Fixed (2026-07-27 — `freshness_class` reaches the REST write path)
- **`POST /api/facts/set` now threads `freshness_class`.** v23 threaded the new
  field through the MCP tool and the service but not the REST route, whose
  lambda passed only entity/attribute/value/confidence/support — so every fact
  written through the Console or the REST fallback was silently pinned to
  `evergreen`. That path is not incidental: it is the documented workaround for
  MCP clients that stringify tool params. `FixtureService.cortex_write` gained
  the same parameter to keep the Console fixture contract in step.
- Two route-level regression tests pin both directions (explicit `volatile`
  survives; omitted stays `evergreen`, never the world cortex's `volatile`).
  RED-checked by removing the kwarg.

### Added (2026-07-27 — cortex facts can declare how fast they rot; schema **v23**)
- **`memory_fact_set` gained `freshness_class`** — `evergreen` (default),
  `slow` (~9 months) or `volatile` (~3 weeks), stored on the fact and
  projected on every read alongside a derived `effective_confidence` and a
  boolean `stale` (past twice the TTL). Non-evergreen facts decay toward a
  per-class floor with time since `last_confirmed`; re-asserting the same
  value confirms the fact and restores full confidence. This reuses the
  existing world-cortex freshness machinery (`memory/freshness.py`) rather
  than adding a second decay model.
- **The default is `evergreen`, deliberately not the world cortex's
  `volatile`.** Personal facts are mostly durable — identities, decisions,
  history — and defaulting the other way would silently re-rank every
  existing bank on an assumption nothing here has measured. Rows written
  before v23 read back as `evergreen`, so behaviour is unchanged until a
  slot is classified on purpose.
- **Scope, honestly.** This does *not* retroactively fix the 2026-07-26
  v1/v2 extractor-prompt incident that motivated the currency work: the
  misleading fact was ten days old against a 21-day TTL, so it would not
  have been flagged `stale`, and its `effective_confidence` would barely
  have moved. Visible dates (previous entry) were the load-bearing fix;
  this is the complementary half that helps once a fact is *months* stale.
  `tests/test_schema_v23.py` records that limit so a later reader does not
  over-credit the feature.
- Guide: [memory model → "How current is this fact?"](docs/guide/memory-model.md)
  now documents both halves — the projected dates shipped in the previous
  entry had no user-facing page, only a CHANGELOG record.
- **Unknown classes fall back to `evergreen` here, not `volatile`.** The
  shared `freshness.normalize_class` sends anything unrecognised to
  `volatile`, which is correct for world facts and inverts the intent on the
  personal cortex — a typo'd class would quietly start a durable fact
  decaying. `cortex._norm_freshness` overrides just that fallback.
- The tool docstring does **not** describe `freshness_class`: the `core`
  tier's 9,500-char manifest budget had ~14 chars of headroom, and the
  manifest is eager context in every session. The `Literal` type advertises
  the three values; the guide carries the meaning.

### Fixed (2026-07-26 — cortex facts now carry their age; mid-session recall codified)
- **`memory_search`'s `cortex` block now returns `asserted_at`,
  `last_confirmed` (ISO-8601, to the second) and the human `age`.** The data
  was already there — `service.cortex_search` has always returned those
  fields — but the MCP tool projected them away, so every cortex fact
  arrived undated and a stale one looked exactly as authoritative as a
  fresh one. Not even `verbose=True` added a date.
- **Why it matters.** The cortex is the layer an agent trusts most, and
  supersession only fires within one `(entity, attribute)` slot — so the
  *same* real fact recorded under a second entity name is never corrected
  and never flagged `contested`. Observed 2026-07-26: a query for the
  deployed extractor prompt returned
  `extraction-prompt-system-prompt/version = "v2"` beside
  `Sonnet sidecar/primary-extractor = "... (v1 prompt)"`, both
  `contested: false`, ten days apart, undated — and an agent in another
  session acted on v1. Second precision so two same-day writes to rival
  slots are still orderable.
- **The briefing now tells agents to recall more than once.** The
  SessionStart hook and `examples/CLAUDE.memory.md` gained a *RECALL AGAIN
  mid-session* list (user refers to work you weren't part of; before
  proposing a design, via `memory_lesson_search` for `polarity:-`
  dead-ends; before asserting a benchmark number, version or "current"
  value; when starting in an untouched area) and an explicit **TRUST
  ORDER**: memory tells you *why*, the repo tells you *what is*. When they
  disagree, say so, trust the code, and correct the fact at its slot rather
  than silently picking one.
- Entity canonicalisation — the root cause, since post-hoc key merging is a
  known dead end (23 true merges over 3,257 keys) — is deliberately left
  for separate work.

### Fixed (2026-07-26 — two defects found by live verification)
- **The retype pass now runs when there is no dream backlog.** It sat at the
  dream tail, so a dream with nothing to consolidate returned early and never
  drained the quarantine — exactly backwards, since the quarantine accumulates
  when dreams are INFREQUENT. Same precedent as lesson synthesis on that path:
  no new memories does not mean no pending work.
- **A git branch is no longer offered as the concept a file implements.**
  `file_concept_split` compares stems with separators and directory prefixes
  stripped, so `terra_shim.py` matched the branch `feat/terra-shim` and the
  queue suggested `implements` — but a branch is a VCS artifact, not a role,
  and a prior review had ruled that exact pair distinct. Branch-shaped
  counterparts are now rejected explicitly.

### Added (2026-07-26 — typed-relation retry over the quarantined edges)
- **Quarantined untyped edges get a second, better-posed question.** The 0.45
  `related-to` quarantine works — a triage of 32 of them found *zero* worth
  writing as-is — but ~44% named a REAL relationship that merely got the
  wrong label (`publishes-to`, `implements`, `operates-on`), and they simply
  accumulated. `retype_quarantined_links` re-asks the extractor for a TYPED
  relation using only the notes where both entities co-occur
  (`graph_consolidation.shared_mention_entries`). Focused evidence plus the
  current prompt — which demands the most specific relation and forbids
  `related-to` for co-mentions — makes this a genuinely different question
  from the one that produced the quarantined edge, with no new prompt surface.
- A typed answer files a **reviewable** `dream-retyped` proposal and settles
  the untyped original; a retype is a second guess on already-suspect
  material, so it never writes a live edge. No typed answer settles the
  original too — that is exactly the co-mention noise the quarantine exists to
  catch, and leaving it pending only regrows the queue. An extractor failure
  settles nothing and never raises.
- Runs at the dream tail, capped by `memory.dream.retype_quarantined_max`
  (default 3, `0` disables). Self-limiting: the pass no-ops on an empty
  quarantine, so a drained bank pays nothing.

### Fixed (2026-07-26 — graph hygiene round 3: four junk faucets closed at source)
- **Flattened slot keys no longer mint dotted entities.** `cortex.vocab()`
  renders hints as `entity.attribute`, but the bare list read as a list of
  *entity names*, so extractors periodically emitted
  `{"entity": "0-9-0-release.deployment-status", "attribute": "value"}` —
  minting a dotted entity duplicating a correctly-shaped fact. Three layers:
  the vocab hint now states the shape explicitly; `unflatten_slot_key_claims`
  repairs what still slips (split only when the attribute is literally
  `value` AND the prefix is a known entity, so `llama.cpp` and
  `host.docker.internal` survive); and `junk_entities` gains a
  `slot-key-artifact` class for existing ones, likewise requiring the prefix
  to be a known entity — the remaining dotted names in the bank
  (`facts.id`, `cms.retrieve`, `np.asarray`) are legitimate code/schema
  references and stay untouched.
- **Lesson-minted nodes are excluded from duplicate and orphan findings.**
  `unattributed()` already skipped entities whose every edge is a
  prefers/avoids lesson relation; the predicate is now shared as
  `lesson_only_ids` and applied to all three. These nodes are named
  `<artifact> <aspect>`, so they share nearly every token with the artifact
  they merely mention — dismissing a pair never stopped the next lesson from
  minting another — and they are weakly connected by construction, which
  inflated the orphan count with entries no action could ever resolve.
- **A contentless entity can no longer be a merge target.** Fold direction
  ranked on degree alone, so a node with no facts and no edges won a 0-0 tie
  by id and absorbed a richly-specified work item ("Atlas graph cleanup"
  repeatedly swallowed real PRs). Both proposal paths — deep-dream
  `partition_candidates` and write-time dedup — now rank on `degree + facts`,
  so a fact-rich node is not out-ranked by one stray edge. Equally-thin pairs
  keep the id tie-break, leaving ordinary bare-vs-path proposals intact.
- **Git branch names type as concepts.** `infer_type` returned `None` for
  `feat/lme-v2-pilot`, and unknown types are deliberately neutral, so
  "llama-server *runs-on* a git branch" scored a clean 0.70 and reached the
  cross-project review queue. Branches now type like `master`/`main`, making
  such pairs a hard type violation that is dropped at the source. The rule
  runs after file-extension typing and uses singular prefixes only, so
  `docs/guide/benchmarks.md` and `evals/ladder_sweep.py` stay files.

### Added (2026-07-26 — `relate` action for file/concept duplicate findings)
- **The review queue can now record an edge instead of forcing merge-or-dismiss.**
  A duplicate finding whose two names are a source file and its own bare stem
  (`band.py` ↔ `band`, `evals/dg_shim.py` ↔ `dg_shim`) now carries
  `action: "relate"` plus a `suggested_relation` (default `implements`), with
  the file listed first so the edge reads `<file> implements <concept>`. Backed
  by a new `POST /api/graph/relate` and a Relate button in the Atlas review
  drawer, which writes the edge and then marks the pair distinct.
  **Why:** these pairs are neither duplicates nor unrelated. The concept
  routinely has identity the file does not — an independent runtime (`dream`
  *runs-on* the host shim, `band` *stores-data-in* postgres, both false of the
  module) or several implementing files (`backup.sh` **and** `ops/backup.ps1`
  realize `Backup`, so no single merge is even well-defined). Merging asserts
  false things about the file; dismissing discards a real relationship — and
  dismissal is permanent. The `implements` relation already described exactly
  this case ("a concrete artifact that realizes an abstract role"); only the
  queue's action vocabulary was missing it. Two-source-file pairs
  (`test_shim.py` / `tests/test_shim.py`) and non-code pairs (`README.md` /
  `README`) keep the ordinary merge action.

### Changed (2026-07-26 — gate baseline re-established at 10 replicates; default raised)

> **Superseded 2026-07-27.** Both the baseline and the raised default below
> were undone once the ~0.03 spread they were sized to average away was
> root-caused to the serving stack rather than the judge — see the
> 2026-07-27 entry. The current baseline is n=7 at std 0.0000, and
> `regression_gate.ps1` defaults to `-Replicates 2`. The numbers in this
> entry are historical.

- **`evals/results/regression_gate.baseline.json` re-established on clean
  `origin/master` (commit `959ecad`) with 10 replicates**, replacing the
  3-replicate baseline from 2026-07-18 that both master and the #38–#44
  stack failed. (An 8-replicate baseline landed 45 minutes earlier the same
  day and was replaced by this one before either shipped; this entry
  originally carried that stale `8` in its title.)

  | arm | mean | std | margin |
  |---|---|---|---|
  | rag | 0.5757 | 0.0230 | 0.0460 |
  | cortex | 0.6692 | 0.0319 | 0.0637 |
  | hybrid | 0.7808 | 0.0165 | 0.0330 |

- **`regression_gate.ps1` now defaults to `-Replicates 10`** (was 3).
  Re-establishing alone would not have helped: the next run that forgot
  the flag would have compared a noisy 3-replicate mean against it and
  false-failed again.
- **The old number sat near the top of the range, not the centre.** Across
  **18 honest replicates** of the identical slice (two independent
  establishes, n=8 then n=10): min 0.6154, mean 0.6674, max 0.7179 —
  **13 of 18 fall below the retired 0.7051 and only 1 exceeds it**. A
  baseline frozen near the best observed run makes every honest run
  afterwards look like a regression, which is exactly what happened.
- The two establishes agree to within **0.004 on every arm** (cortex
  0.6651 at n=8, 0.6692 at n=10) — the evidence that the estimate is now
  stable rather than another lucky draw.
- **A zero-variance baseline silently disables the gate's calibration.**
  `make_baseline` sets `margin = max(0.03, 2 x std)`, so the old 0.03 was
  the floor showing through rather than a chosen value. Guarded now by
  `tests/test_regression_gate_defaults.py`, which also pins the default
  replicate count and that the baseline was established at ≥ that count.
- Both earlier runs pass the new baseline on all three arms, including
  the **#38–#44 stack** — the gate obligation on those changes is
  discharged.
- Cost, since the gate re-runs its replicate count every invocation:
  ~3–3.75 min per replicate, **~32–37 min for 10** (measured 28 for 8, 37
  for 10; the spread is host load). At the old n=3 the margin was ~1.2
  standard errors of the difference — roughly a 1-in-5 false-fail rate.
  Note the margin barely moves with N (it is `2 × std`, and std estimates
  a spread that does not shrink); more replicates buy a better estimate of
  the *mean* on both sides.

### Fixed (2026-07-26 — the regression gate's baseline is stale, not the code)
- **`evals/regression_gate.ps1` fails on clean `origin/master`.** Cortex
  **0.6709 ± 0.0370** against a committed baseline of **0.7051**. The
  #38–#44 stack gives **0.6581 ± 0.0196** — **0.0128** from master, well
  inside the noise. The changes did not cause the failure.
- Corroborated three ways. The gate copies the **rag** arm's context
  verbatim, so its two runs have *identical inputs* and still differ by
  **0.021** — that is the judge's noise floor, and it is larger than the
  cortex delta under test. The cortex rebuild path (`rebuild_contexts.py`)
  is plain cosine over dumped fact banks and never touches band retrieval,
  BM25, the recency prior or `detect_contradictions`, so no change in the
  stack can reach it. And `CORTEX_TOP_K` / `CORTEX_MIN_SCORE` are untouched
  by all 90 commits since the baseline.
- **Why the baseline is wrong**: its recorded `std` is exactly **0.0** on
  two arms — three LLM-judge replicates returning identical accuracy —
  where today's runs show 0.007–0.037. It predates
  `LLAMA_ARG_CACHE_RAM=0`, so prompt caching plausibly suppressed the
  variance that has now returned. It also disagrees with `evals/README.md`'s
  own 5-replicate measurement of the same slice, **cortex 0.682 ± 0.017**;
  both of today's runs sit closer to that than to 0.7051.
- **The gate is also under-powered**: a 0.03 margin against a cortex std of
  0.037 at n=3 is ~1.4 standard errors, so it fails a meaningful fraction of
  runs with no change at all. Re-establishing it wants more replicates and a
  margin derived from the measured spread — and deliberate promotion, not a
  side effect of a rerun. **Not re-established here.**
- Both runs committed as evidence:
  `regression_gate-2026-07-26-master-control.agg.json` and
  `regression_gate-2026-07-26-stack-38-44.agg.json`.

### Added (2026-07-26 — embedder comparison on our own corpus)
- **`evals/embedder_recall.py`** ranks every haystack turn of a
  LongMemEval `s` question against the question text and reports recall@k
  of the turns marked `has_answer`, plus an exact paired **McNemar** test
  between backbones. Pure retrieval — no reader, no judge, CPU-only.
- Result (150 questions, 74,183 turns, **299 gold turns**; artifact
  `evals/results/embedder-recall-comparison.json`):

  | backbone | R@5 | R@10 | R@20 | dim |
  |---|---|---|---|---|
  | `all-MiniLM-L6-v2` (shipped) | 0.408 | 0.572 | 0.756 | 384 |
  | `BAAI/bge-base-en-v1.5` | **0.559** | **0.742** | **0.853** | 768 |

  Paired: **+54/−9 at k=5** (p=6e-09), **+60/−9 at k=10** (p=2e-10),
  **+34/−5 at k=20** (p=2e-06). A ~17-point gain at k=10, roughly 6 wins
  per loss.
- Costs, for the migration decision: ~10× CPU encode time (72 ms vs 7 ms
  per text, un-tuned — an ONNX backend is already wired for MiniLM) and 2×
  the stored vector width. `vector(384)` is declared in four tables, so
  adopting it is a schema migration plus a re-embed of every row.
- **A 30-question pilot was discarded as worthless, for two independent
  reasons** worth recording: the machine was busy (invalidating the timing
  column), and the first 30 questions in file order are far easier than the
  corpus — R@5 0.806 there against 0.408 over 150. Near ceiling the two
  backbones look identical; the whole difference was one document. Do not
  take the head of this dataset as a sample.

### Fixed (2026-07-26 — the deploy scripts promised a rollback they hadn't made)
- **`ops/update.ps1` and `ops/update.sh` only print a rollback command when
  the rollback tag actually exists.** Both computed `$rollback`
  unconditionally but only `docker tag`-ged it when a current image was
  found; when it wasn't — a first build, or a version bumped before that
  version was ever built — the tag was skipped with a warning and both exit
  paths printed `docker tag <rollback> <image_tag>` anyway.
- Observed live on 2026-07-26: the deploy warned *"No current
  pseudolife-daemon:0.10.0 image to tag"* and then printed a rollback
  command naming a tag that does not exist. **The unhealthy path is the
  dangerous one** — the operator reaches for that command precisely because
  the deploy just broke, and it fails.
- Now: the no-image case says so explicitly at tag time *and* in the
  rollback block, and offers the path that does work (rebuild the last-good
  ref). The tagged case is unchanged.
- Covered by `tests/test_ops_update_rollback.py`, which drives the real
  script with `docker` and the health probe stubbed as PowerShell
  functions, in both the healthy and unhealthy branches.

### Added (2026-07-25 — supersession on slot identity, not embedding similarity)
- **`detect_contradictions` gained a slot-identity path, checked first.**
  When the new text and an existing entry assert different values — or
  opposite polarities — at the same normalised `(entity, attribute)` slot,
  the old entry is superseded. Deterministic and embedding-free, using the
  same `_norm_key` / `_norm_value` normalisation the cortex uses, so the
  two stores agree on what counts as the same slot.
- Why not cosine: the three existing paths all gate on similarity, which
  is a weak discriminator here. A value swap is a *minimal* edit, so a real
  contradiction is often more embedding-similar than a harmless
  near-duplicate — independent measurement puts cosine at AUROC 0.59 for
  this judgment, barely above chance. The new path reaches corrections at
  any similarity, which is where the cosine-gated paths cannot look at all.
- A matching key with the same value **and** polarity is a restatement, not
  a correction, and is deliberately not flagged — restatements are exactly
  what knowledge-update evidence looks like.
- **Coverage is bounded by slot extraction, which is precision-gated:**
  measured on 6,000 real conversation turns, only **0.63%** yield any slot,
  and the path needs slots on both sides. It therefore targets deliberate
  fact-shaped writes far more than replayed chat, and its reach will grow
  with extractor quality rather than with tuning here.
- Slot extraction now runs **once** per store instead of twice — it was
  being done after the write, and the contradiction scan needs it before.

### Changed (2026-07-25 — writes no longer rescan the bank for possession cues)
- **Contradiction detection caches its gain/loss cue flags per entry.**
  `detect_contradictions` runs over every entry of every band on every
  write, and its state-transition path did four regex-list scans per
  entry before consulting similarity. Profiling a saturated store put
  **94%** of the time there — 1.58M regex searches across five stores —
  against ~4% for the capacity-eviction path that was assumed to be the
  cost. The cue check is that path's early exit, so for the overwhelming
  majority of entries it *is* the work. `MemoryEntry.cue_flags` memoises
  it (transient, never persisted, excluded from equality; sound because
  `text` is never mutated after construction), and the new text's cues are
  hoisted out of the loop.
- Median store latency, real MiniLM embeddings and real conversation text
  (`evals/bench_store_latency.py`, artifact
  `evals/results/store-latency-by-bank-size.json`):

  | resident entries | before | after |
  |---|---|---|
  | 500 | 224 ms | **4.3 ms** |
  | 5,250 (summed capacity) | 1,763 ms | **11.2 ms** |
  | 9,000 (over capacity) | 3,277 ms | **22.5 ms** |

- Cached on the entry rather than in a process-wide LRU deliberately. Path
  3 sweeps the whole bank per write — a cyclic scan, the pathological case
  for LRU, which is 100% hit while the resident set fits and 0% the moment
  it doesn't, with no gradient. An `lru_cache(maxsize=8192)` measured
  identically up to 8,190 resident and then reverted to **1.01x** at
  8,300. The resident set is not reliably bounded (`rebalance_bands`
  leaves the deepest band over capacity rather than truncating at startup,
  and the shipped preset invites raising `max_entries`), so a fixed size
  would have been a cliff. Pinned by a test that would fail on any
  bank-size-dependent warm cost.
- Also rejected: gating the anchor scan on a minimum similarity floor.
  Provably equivalent and worth 5.1x alone, but subsumed by the cue cache
  — and it measured *slower* alongside a process-wide cache, because
  scanning ~24% of the bank per store let resident texts age out. Recorded
  at the call site so it isn't re-attempted blind.

### Fixed (2026-07-25 — restore paths ignored band capacity)
- **`hydrate_cms`, `load()` and the legacy migration now rebalance once.**
  All three append straight to `band.entries`, bypassing `store()` and its
  capacity check, and `hydrate_cms` additionally routes rows whose band no
  longer exists into `bands[0]`. A preset rename could therefore pile the
  whole bank into the 200-slot `working` band — and because every store
  then evicts one entry and appends one, it never drained: the band stayed
  full permanently with every store scanning all of it.
  `ContinuumMemorySystem.rebalance_bands()` walks shallow → deep once,
  spilling each band's lowest-scoring surplus into the next by the same
  retention policy `_evict_one` uses. Measured on the shipped 8-band
  preset: 5,250 rows all stamped with a dead band name seat correctly in
  **0.30 s**, no band over capacity.
- Harmless before 2026-07-25, when capacity eviction deleted and the
  resident set stayed far below capacity (~212 rows against 5,250 on a
  realistic corpus). Demote-on-evict pushed the resident set to the summed
  capacity, which is what made this reachable.
- **The deepest band may finish over capacity** when the bank holds more
  rows than the preset seats. That is deliberate: startup is the wrong
  place to destroy memories the user did not ask to lose. It is logged
  with the count, and drains through the normal eviction path.
- Known cost, not introduced here but now reached sooner: with every band
  saturated, a store cascades an eviction through all eight, each scoring
  its whole band — **~450 ms** at 5,250 resident against ~18 ms at 500.
  The pre-fix pile-up measured ~330 ms but never converged. Reducing that
  scan is tracked separately; a bank near capacity is the only place it
  bites.

### Fixed (2026-07-25 — a failed band move could leave one entry in two bands)
- **`_consolidate` now prunes the source band in a `finally`.** It moved
  entries in a loop but pruned only after finishing, so a raise partway
  through left every already-moved entry live in *both* the source and the
  destination, sharing one `db_id`. Retrieval hid it (dedup is by
  `entry.text`), but `memory_stats` over-counted and the next
  consolidation relocated the stale copy onto the same row. Reproduced at
  4 entries: bands held 6 rows for 4 distinct memories.
- **`_relocate` is all-or-nothing.** It is the shared move primitive for
  promotion *and* for capacity demotion, and both callers prune the source
  on the strength of it returning — so a half-applied move is exactly the
  duplicate above. A failure after the destination append now rolls that
  append back before propagating. (`band.store` itself cannot fail after
  appending — only attribute assignment follows — so the reachable window
  is the provenance copy, which is what the rollback covers.)
- Both were pre-existing; the eviction change earlier today made
  `_relocate` a hot path, which is what made the window worth closing
  rather than narrowing.

### Fixed (2026-07-25 — band overflow destroyed memories; depth was faking recency)
- **Capacity eviction now demotes to the next band instead of deleting.**
  A band at capacity handed its lowest-scoring entry to `/dev/null` along
  with its Postgres row, while the only *other* exit from a band —
  promotion — requires `access_count >= N or surprise > threshold`. So an
  unsurprising, never-retrieved entry died in the 200-slot `working` band
  with ~5,050 slots free behind it. Measured on the LongMemEval `s`
  replay: **31.1% of stored turns discarded at 6.4% total capacity
  utilisation**, `working` saturated in 78/78 questions, `forever` empty
  in all 78. Answer-evidence turns fared *worse* than average (**37.5%
  evicted vs a 31.1% base rate**, with 58% of questions losing at least
  one) because eviction ranks on novelty and knowledge-update evidence is
  a restatement, hence unsurprising by construction. Overflow past the
  deepest band is still a real drop, so summed capacity remains the bound.
- **The depth-ramped recency boost is off by default**
  (`memory.recency_boost_enabled = false`). Retrieval scaled scores by a
  `0.4 → 0.0` ramp over band depth, treating depth as a proxy for age —
  but depth is set by promotion history, which without retrieval to
  accrue access counts tracks surprise rather than age. The ramp could
  therefore rank a weaker match in `working` above a stronger match in a
  deeper band; measured cost up to **18 points on the LongMemEval
  naive-RAG arm**. Set the flag to `true` for the previous ranking.
- **An explicit `min_score` now bounds BM25-only injections.** They enter
  the pool at `weight × normalised_bm25`, bypassing the dense pool's
  relevance gate entirely. The default floor deliberately still does not
  apply to them (injected scores are ≤0.3 at the shipped weight, so 0.25
  would admit only the top lexical hit per query), but a caller-supplied
  floor is a contract over the whole result set.

### Changed (2026-07-25 — BM25 hybrid retrieval is on by default)
- **`memory.bm25.enabled` now defaults to `true`.** The lexical pool
  shipped disabled, so every published retrieval number — including the
  LongMemEval knowledge-update figures and the band ablation — measured
  dense-only retrieval through a 22M-parameter 2021 bi-encoder. It is
  pure stdlib, adds no dependency, and costs ~20–50ms per query at bank
  scale. Opt out with `memory.bm25.enabled = false` or per-call
  `bm25=False`.
### Fixed (2026-07-25 — the llama-server eval crashes, root-caused and fixed)
- **Every eval harness that starts the Qwen judge/answerer now disables
  context checkpoints** (`LLAMA_ARG_CTX_CHECKPOINTS=0`). The fork's
  create/restore/erase churn around the 149.6 MiB recurrent-state
  checkpoints that Qwen 3.6's hybrid Gated-DeltaNet layers trigger per
  task leaked **~285 KV cells per request**, emptying the `-c` pool at a
  rate set by request *count* — hence `decode: failed to find a memory
  slot`, then `GGML_ASSERT(offset + size <= ggml_nbytes(tensor))` and a
  `0xC0000409` abort. Six overnight runs died in a tight 345–377 request
  band; a 600-request soak with checkpoints off passed at 1.7× that, with
  flat latency (1.8–2.2 s), flat VRAM, and zero memory-slot errors.
  Checkpoints buy eval workloads nothing — these prompts are
  full-reprocessed either way, and MTP speed/quality were unchanged.
- `LLAMA_ARG_CACHE_RAM=0` is set alongside it. This was the *first*
  diagnosis and it was **wrong** — the next run crashed six times with
  the cache verifiably disabled. The flag stays only because the prompt
  cache is pure overhead for eval prompts, not because it fixed anything.
- Both are env vars set in each script's `Start-Qwen`, never CLI flags in
  `run-server-turboq.bat`: interactive use of the same server depends on
  warm cache hits.
- Each `Start-Qwen` also archives the previous `qwen-server.log` to
  `crash-logs/` before the `>` redirect truncates it. The abort message
  lives in the tail of the *old* log, which every prior launch destroyed —
  which is a large part of why this took two rounds to root-cause.
- Applied uniformly to all ten harnesses with a `Start-Qwen`:
  `bench_diffusiongemma`, `gate_e4b_ft`, `gate_window`,
  `overnight_longmemeval`, `overnight_qat_ornith`, `overnight_replicates`,
  `regression_gate`, `tonight_bakeoff`, plus `overnight_lme_v2` and
  `overnight_band_wabl`.

### Fixed (2026-07-25 — the V2 reanswer path resumes instead of restarting)
- **`lme_v2_smoke.reanswer` now resumes from its own JSONL** (append +
  skip-done), matching `run_smoke`. It previously opened the output with
  `"w"` and re-read every row from source, so each retry restarted from
  row 1: on 2026-07-25 six server crashes discarded ~29 minutes each and
  turned ~15 minutes of compute into 3.6 hours, with only the single
  crash-free attempt ever producing a file. A crash now costs one
  question.
- **Rows record their `answer_prompt` (`ku` / `compose`), and a resume
  across a prompt change is refused.** Resuming made a mixed-prompt file
  possible where truncation had made it impossible (same `--out-tag`,
  different `--answer-prompt`, half the rows answered under each); the
  run now exits and names the clash. Rows written before the label
  existed are unlabelled and treated as compatible.
  Pinned by `tests/test_lme_v2_smoke_resume.py`.

### Changed (2026-07-25 — the 8-band continuum loses on the write side too)
- **The continuum's last defence is gone.** The 2026-07-19 ablation showed
  the banding earns nothing on *ranking* and left the write side
  (eviction, capacity, cadence) as the remaining case for it. The
  write-side ablation now measured that too — flat INGEST at the
  continuum's total capacity (5,250) vs the stock 8 bands, `s` dataset,
  5 replicates, paired permutation over 78 questions — and the continuum
  **loses**. Write-side isolation (identical flat ranking, only the
  survivor sets differ): hybrid −0.110 p=0.018 (`wall`), −0.108 p=0.027
  (`hist`); rag −0.090/−0.097, n.s. Whole system: all four significant,
  rag −0.274 p=0.0001, hybrid −0.141 p=0.0038. Mechanism: at ~488
  turns/question the 200-entry `working` band overflows faster than
  promotion drains it, so the continuum **evicts 31.1% of everything
  stored** while a flat pool of equal total capacity evicts nothing.
  Bounded honestly in the docs: the flat arm never evicts on this corpus,
  so this measures partition-forced eviction vs none, not one eviction
  *policy* vs another. Published in `docs/guide/benchmarks.md` and
  `evals/README.md`; every delta and p-value pinned to a committed
  artifact by `tests/test_eval_evidence.py`.
- **The LongMemEval-V2 pilot's headline claim is retired.** The
  10-question slice concluded *hybrid beat both single channels in every
  replicate under both prompts*; the complete 74-question `procedure`
  category does not reproduce it — hybrid leads under the default prompt
  (0.243 vs rag 0.162) but **ties naive RAG under the composition-aware
  prompt** (0.284 each). The pilot's ten questions were the first ten in
  dataset file order and proved far easier than the category as a whole
  (every arm roughly halves). Superseded at the site a reader meets it,
  per the retirement rule; the pilot numbers are retained inline as
  history.

### Added (2026-07-24 — write-side band ablation + overnight harness pair)
- **evals**: `band_ablation.py` grew the write-side arm the read-side
  ablation could not express: `replay --band-preset flat` re-runs ingest
  through ONE flat band at the continuum's total capacity (5,250, or
  `--flat-cap`), injected via a `config.yaml` the service reads at
  construction and verified loudly (band count, `surprise_threshold`
  pinned so the arms' configs differ **only** in `memory.miras` —
  `tests/test_band_ablation_flat.py` proves the invariant). `rebuild
  --band-preset flat` emits `wabl-flat-{wall,hist}` answer-phase JSONLs
  plus a survival-stats artifact (per-question stored/survivor counts per
  arm). Only meaningful on the `s` dataset: the 2026-07-24 probe measured
  ~28% of stored turns evicted by the continuum there vs 0% flat, while
  `oracle` (~23 turns/question) never evicts. `--src-tag ""` now
  addresses untagged source runs (previously produced malformed
  double-dash filenames).
- **evals**: overnight harness pair for the two open research threads —
  `overnight_lme_v2.ps1` (full 74-question V2 procedure slice, supervised
  llama-server restarts, JSONL-cursor resume), `overnight_band_wabl.ps1`
  (`-Phase cpu` replays/rebuilds alongside a GPU night on private bench
  DBs; `-Phase answer` for the follow-up GPU window, compares written
  with `--out` artifacts), plus `preflight_overnight.ps1` and
  `overnight_status.ps1` (heartbeat/ledger checks). Runbook:
  `docs/runbooks/overnight-band-wabl-and-lme-v2-slice2.md`.

### Changed (2026-07-21 — the extractor ladder refuses to clobber canonical results)
- **`evals/ladder_sweep.py` never overwrites an existing result file in
  place.** An untagged rerun of a rung whose `evals/results/<rung>.json`
  already exists now exits up front — *before* the hours-long run starts —
  with instructions to pass the new `--out-tag <tag>` flag, which writes
  `<rung>-<tag>.json` alongside the canonical file for deliberate promotion.
  The same guard covers the `--abstain` / `--supersede` sub-sweep outputs.
  This is the code-level enforcement of the "never overwrite a canonical
  result file on a rerun" rule, written after a rerun silently rewrote
  `sonnet-5.json`'s timing fields in place (2026-07-21); pinned by
  `tests/test_eval_ladder_sweep.py`.

### Fixed (2026-07-21 — the auto-started daemon no longer steals foreground focus)
- **`shim.spawn_daemon` now spawns with `CREATE_NO_WINDOW`, not
  `DETACHED_PROCESS`.** Both keep the daemon off the caller's console, but
  `DETACHED_PROCESS` leaves the child *needing* a console, and Windows 11
  hands that allocation to the configured default terminal app — Windows
  Terminal then opens a real window that takes foreground. Every shim
  auto-start cost the user a stolen window, in tests and in daily use alike.
- The 2026-07-20 pass (#24) treated this as a test-harness problem and added
  `CREATE_NO_WINDOW` to the three `subprocess.Popen` daemon spawns in
  `test_daemon_http.py` / `test_writer_keying.py`. Those spawns were never
  the source: the windows came from the *shipped* shim, which the suite
  reaches through `test_shim.py`'s three auto-start tests. Instrumenting a
  real suite run with a window watcher attributed all three focus-stealing
  `WindowsTerminal.exe` windows to that one path; a flag matrix over the
  exact `spawn_daemon` call then measured `DETACHED_PROCESS` → 2 visible
  windows per spawn and `CREATE_NO_WINDOW` → 0, with detachment (the child
  outliving its spawner) confirmed intact. Same conclusion
  `ops/install-shim-autostart.ps1` reached live on 2026-07-12 for the Sonnet
  shim — the shipped Python spawner had simply never been brought in line.
- Pinned by two tests in `tests/test_shim.py`: a behavioural one asserting
  the flags `spawn_daemon` actually passes, and a package-wide guard that
  fails on any `DETACHED_PROCESS` *use* in `pseudolife_memory/` (comments
  explaining the choice are tokenized out, so documenting the flag stays
  legal). The guard exists because the previous fix's site-by-site scope is
  exactly what let the real caller slip through.

### Added (2026-07-21 — published benchmark numbers are pinned to committed evidence)
- **`tests/test_eval_evidence.py` fails the suite when a published number has
  no committed artifact behind it.** Every benchmark claim in the README and
  `docs/guide/benchmarks.md` now names the result file it came from, and the
  guard asserts three things: the artifact is git-tracked (a working-copy-only
  file counts as missing, which is exactly what a fresh clone sees), its value
  matches the doc to the published precision, and the guarded doc text still
  exists — without that last check a reword would leave the guard green and
  guarding nothing. Whether a number is *right* still needs a GPU and stays a
  human gate; whether it is *backed* is pure parsing, so it runs here.
- **The band-ablation evidence is now in the repo.** Its significance claim
  (naive RAG under `hist`, −0.090 at p = 0.015) rested on 16 untracked
  replicate files, four tracked base files carrying no scoring fields at all,
  and no comparison artifact whatsoever — one `git checkout .` from being
  unreproducible anywhere. All 40 files plus six generated comparisons are
  committed; every one reproduces the published table exactly.
- **`replicate.py compare` and `lesson_synthesis_bench.py` take `--out`.** Both
  printed their results and forgot them, which is how the `--infer` rung's
  scores reached this changelog with nothing standing behind them. Comparison
  artifacts also record `permutations` and `seed`, without which a permutation
  p-value cannot be re-derived.

### Fixed (2026-07-21 — a double-rounded standard deviation on the front door)
- **The local-ceiling table published `cortex 0.559 ± 0.030`; the aggregate
  says `0.02949…`, which rounds to `0.029`.** Corrected in the README and
  `docs/guide/benchmarks.md`. The evidence guard above found it on its first
  run, against a number nobody had thought to question.

## [0.10.0] - 2026-07-21 — documented vs enacted: the dream pass learns what your documents prescribe

The dream pass now captures **what a document you shared prescribes** as a
durable fact under that document's subject, kept distinct from what was
actually done — so a runbook's rule and a deploy that skipped it become two
facts, not one blurred into the other. This lands on both extractor tiers
(the bundled E4B sidecar and the Sonnet override prompt), each gated on its
own deployed artifact.

The change came out of a LongMemEval-V2 pilot where every arm scored 0.000
until we found that our own extraction prompt — "extract exactly two kinds
of claim and nothing else" — was making an obedient model silently discard
the protocol documents the answers were drawn from. Also in this release:
the Console's store-curation review panel, deep dream's lesson/world
cross-key duplicate curation, and a hybrid-retrieval default that the same
benchmark work validated (cortex facts ahead of associative recall beat
either channel alone in every replicate).

### Changed (2026-07-21 — Sonnet extractor prompt v2: DOCUMENTS PRESCRIBE)
- **`evals/prompts/sonnet_extractor_v2.md`** — v1 plus one section applying
  the LME-V2 Fix-E lesson to the Sonnet override: what a quoted/summarized
  document (spec, policy, protocol, runbook, guide) prescribes is itself a
  durable fact, extracted under the document's subject even when other notes
  show different enacted behavior. Gated on the ladder `sonnet-5` rung:
  metric-identical to v1 (`gold_recoverable` 1.0, `stale_leak` 0.0, 16/16
  claims — results committed as `ladder-sonnet5-prompt-{v1,v2}.json`), and a
  positive probe confirmed the section fires (documented rule + divergent
  enacted behavior extracted as separate facts). The autostart installers
  (`install-shim-autostart.ps1`/`.sh`) and install-script hints now default
  to v2; running deployments pick it up by re-running the autostart
  installer (or restarting the shim with the v2 `--system-prompt-file`).

### Changed (2026-07-20 — extraction prompt learns the LME-V2 lessons; hybrid default pinned)
- **The shipped extraction prompt (`_SYSTEM_PROMPT`) now names document
  prescriptions as extractable and carries a worked example.** Two transfers
  from the LongMemEval-V2 arc: (1) a scope-restricted extraction prompt makes
  an obedient model silently discard content classes it doesn't name — the
  prompt now states that what a shared DOCUMENT (spec, policy, protocol,
  runbook, guide) prescribes is itself a durable fact, distinct from what the
  session did; (2) small extractors follow a demonstrated format far more
  reliably than imperative instructions — a compact worked example shows a
  current-state update plus a `documented requirement` claim. Gated on the
  ladder's `e4b-ft` rung (the deployed Arm-1 student): `gold_recoverable`
  1.0 → 1.0, `stale_leak` 0.0 → 0.0, extract time flat — and claim output got
  *cleaner* (16 claims / 16 inserts vs the baseline's 26 claims for the same
  16 inserts; the example removed redundant duplicates). The datagen path
  reads the same constant, so the next distillation cycle trains on the new
  prompt automatically. `extractor_max_tokens` stays 2048 — the LME-V2 A/B
  showed 4096 scores worse (extra deliberation rope hurts small models more
  than truncation).
- **Hybrid retrieval's defaults are now test-pinned** (`cortex.enabled` and
  `cortex.search_first` both `True`, new `tests/test_hybrid_default.py`). The
  LME-V2 procedure-slice replicates showed the hybrid context beating both
  single channels in every replicate under both answer prompts; the defaults
  were already correct — the pin makes flipping them a deliberate,
  test-visible decision.

### Changed (2026-07-20 — LongMemEval-V2 Fix E: documented-protocol extraction + answer-format anchoring)
- **LME-V2 smoke: third extraction claim kind — DOCUMENTED PROTOCOL** (evals-only).
  Fix D put the article body in front of the extractor, but the Fix-B trajectory
  prompt said "exactly two kinds of claim and nothing else" (click-path
  workflows + affordances), so the extractor *correctly* discarded document
  prose — and `procedure` gold answers follow the documented protocol
  (Reports → Problems), not the agent's enacted clicks (Knowledge Search →
  Problems). `[article]` notes now yield an
  `entity=<protocol title> / attribute='documented procedure (modules in
  order)'` claim mapping prose steps to canonical navigator module names.
- **Compose answer prompt: protocol authority + worked example + headroom.** A
  named protocol's documented procedure is declared authoritative over enacted
  click-paths; the format section now cooperates with the benchmark's own
  `\boxed{}` convention (the scorer extracts boxed text first) instead of
  forbidding explanations qwen ignored anyway, anchored by a tiny worked
  example showing duplicate-module dropping and boxed termination; compose
  answers get `max_tokens=2048` (256 truncated mid-reasoning).
- **Result (question `025db8ef`, full 100-trajectory haystack, bm25+rerank+
  lexical-cortex):** all three arms flip 0.000 → **1.000** on both the
  deterministic scorer and the judge (`fixe-compose3` tag). Cortex/hybrid
  answer straight from the documented-procedure fact in 3–4 s; rag composes
  the same answer from raw task turns in ~25 s. The n=1 diagnosis chain is
  closed; next step is the wider procedure-question slice.
- **10-question procedure slice (`slice1` / `slice1-compose` tags,
  bm25+rerank+lexical-cortex, 100-trajectory haystacks):** deterministic
  accuracy KU prompt rag 0.30 / cortex 0.30 / **hybrid 0.60**; compose prompt
  rag 0.50 / cortex 0.30 / **hybrid 0.70** — from 0.00 across every arm
  before Fixes A–E. Hybrid ≥ both single channels on both prompts; V2
  procedure hybrid 0.70 is in line with the V1 knowledge-update hybrid oracle
  (0.705 — a since-retired unreplicable single run; the replicated band is
  0.695 ± 0.017, see the 2026-07-18 entry below). One llama-server crash mid-run (WinError 10054) was caught by the
  probe-gated abort and the run resumed losslessly from its per-question
  JSONL cursor.
- **3-replicate aggregate (`slice1` / `-r2` / `-r3`, `slice1.agg.json`):**
  KU prompt rag 0.300 [0.30–0.30] / cortex 0.167 [0.00–0.30] / **hybrid
  0.533 [0.50–0.60]**; compose prompt rag 0.500 [0.40–0.60] / cortex 0.233
  [0.10–0.30] / **hybrid 0.633 [0.60–0.70]**. Hybrid beats both single
  channels in every replicate under both prompts; rag is the most stable
  arm, cortex the most run-to-run volatile (extraction nondeterminism —
  llama-server generation varies across runs even at temperature 0). A
  4096-token answer A/B (`slice1-compose4k`) scored WORSE than 2048 (hybrid
  0.60 vs 0.70) — extra deliberation rope hurts more than truncation; the
  compose cap stays 2048. Remaining failure classes are content-class
  limits, not harness bugs: form-field-inventory MC (`07ffeedf`),
  click-level navigation-detail MC (`100ff132` — detail the extraction
  prompt deliberately drops as non-durable), and cross-workflow comparison
  with non-converging deliberation (`4df5e6b4`). llama-server died 4× across
  the replicate campaign (~60–90 min sustained-ingest pattern); every crash
  was caught by the probe-gated abort and resumed losslessly.

### Changed (2026-07-20 — LongMemEval-V2 Fix D: capture knowledge-article body text)
- **LME-V2 adapter now captures ServiceNow KB *article body text*** (evals-only;
  no product behavior change). Fix A distilled each state to title + landmark
  labels + resolved action and deliberately dropped body StaticText — but some
  `procedure` gold answers are grounded in the BODY of a "Company Protocols"
  knowledge article (e.g. "Agent Workload Balancing" prescribes "...access the
  list of reports... Re-assign the ... problem..."), so that prescription never
  entered the corpus and no extractor could recover it.
  `trajectory_to_turns(include_observations=True)` now detects article pages (a
  `RootWebArea` titled "… Knowledge Portal" that carries an `article` role node —
  matches exactly the five article pages across all 200 small-tier trajectories,
  excludes every "Knowledge Search"/"Knowledge Home" chrome page) and emits each
  distinct article's body ONCE per trajectory as a framed `[article] <title>:
  <body>` turn, appended right after the step where it first opened. Body text
  is the `article` subtree's StaticText/heading/link names in document order
  (links stay interleaved so sentences read coherently; repr-style quoting with
  escaped apostrophes is parsed correctly), boilerplate (KB number / "Authored
  by" / views / "Copy Permalink") dropped, capped at `article_chars` (1500).
  Gated by the new `include_article_body` flag (default on; rides the
  observations path). For question `025db8ef` (full 100-trajectory haystack) the
  phrase "list of reports" goes 0 → 1 and "Re-assign" 2 → 4, at 1.005× the Fix-A
  corpus (article pages are rare — 8 turns over 5 articles). New
  `evals/lme_v2_check_fixd.py` gates the corpus rebuild offline (CPU-only).

### Changed (2026-07-19 — LongMemEval-V2 pilot CPU fixes: trajectory ingest recovers gold labels)
- **`OpenAICompatExtractor` gained an optional `system_prompt` argument**
  (default `None` → the shipped `_SYSTEM_PROMPT`, so the daemon and every
  product path stay byte-identical). Off-label harnesses can now supply a
  domain-specific base extraction prompt while still getting the vocab /
  known-facts hints appended. Used by the LME-V2 smoke's trajectory-mode prompt.
- **LME-V2 evals harness (`evals/lme_v2_*`) — three post-mortem fixes** (all
  evals-side; no product behavior change beyond the extractor arg above):
  *Fix A* — `trajectory_to_turns(include_observations=True)` no longer dumps raw
  `accessibility_tree`s (which were ~47× the baseline corpus and, being opaque
  bids, contained **zero** occurrences of the gold module names). It distils
  each state into a resolved action label (bid → node role+name, resolved
  against the pre-action tree) plus a capped page context (title + headers). For
  question `025db8ef` the gold term "Problems" goes 0 → 10 occurrences at 1.43×
  the baseline size. *Fix B* — a trajectory-mode extraction prompt that pulls the
  ordered workflow + environment affordances instead of durable user facts.
  *Fix C* — a cross-trajectory synthesis pass clustering same-task procedure
  claims into a canonical "typical workflow" fact, weighting `outcome=success`
  over `failure`. New `evals/lme_v2_check0.py` gates the corpus rebuild.

### Added (2026-07-19 — Console: store-curation review panel in the Atlas drawer)
- **The lesson/world duplicate listings are now reviewable in the Console.**
  The Atlas Review drawer gains a "Store curation" panel rendering each
  `lesson_duplicates` / `world_duplicates` pair with both sides'
  entity/attribute/value (plus polarity/outcome/about for lessons and a
  scheme-guarded `source_url` link for world facts) and a confirm-gated
  "Mark distinct" button posting to `POST /api/curation/dismiss-duplicate`.
  Backed by a new standing `GET /api/curation/duplicates` (service
  `curation_duplicates`) that computes the same pairs as the deep dream —
  shared listing helper, so thresholds/dismissals can't drift — without the
  graph-wide dream pass, so the drawer loads them on demand and dismissals
  take effect immediately. Only the distinct verdict is actionable in the
  UI; true duplicates are still settled agent-side via `memory_forget`
  (nothing is ever auto-deleted).

### Added (2026-07-19 — deep dream: lesson/world cross-key duplicate curation)
- **`memory_dream(action="deep")` now lists curation candidates for the
  lesson and world stores** — `lesson_duplicates` / `world_duplicates` in
  both dry-run and apply responses. Dedup/supersession in those stores is
  strictly per-slot (`(task, aspect)` / `(entity, attribute)`), so
  near-duplicates parked under different keys accumulated silently (a
  2026-07-19 manual sweep found 6 duplicate lesson groups and 1 duplicate
  world slot). The new `graph_consolidation.slot_duplicate_candidates`
  reuses the deep dream's cosine candidate-pair approach over the records'
  own embeddings (floor/cap via the new `memory.deep_dream`
  `curation_min_similarity` = 0.80 / `curation_top_k` = 20). Listing-only:
  nothing is auto-deleted — duplicates are settled with the existing
  `memory_forget` tools, and genuinely-distinct pairs are dismissed via the
  new `memory_graph_review(action="dismiss_slot_pair")` /
  `POST /api/curation/dismiss-duplicate` (service
  `curation_dismiss_duplicate`), persisted in the existing
  `dismissed_pairs` table under a `lesson:`/`world:` namespace (safe
  because `graph.norm_name` strips `:` while every curation row carries a
  colon-bearing prefix, so graph-name dismissals can't collide; literal
  `|` in a slot component is folded to `-` to keep the joined key
  unambiguous; no schema change).

### Changed (2026-07-19 — installer no longer prompts for the CLAUDE.md block)
- **The standing memory-loop block is opt-in, not a prompt.** The installer's
  "Append the memory-loop block?" question (default Y) double-injected: the
  session-hook briefing already delivers the byte-identical block every
  session, so accepting the default cost ~60 duplicated lines of context per
  session. The interactive prompt is removed; the default is skip, with the
  one-liner printed for anyone who wants the standing copy. Explicit
  `--instructions append` / `-Instructions append` (and the `--claude-md` /
  `-ClaudeMd` compatibility aliases) still write it — useful for subagent
  visibility (subagents read CLAUDE.md but not hook output) and hook-less
  setups. i18n source bumped to v5 for the quickstart-narrative change.

### Fixed (2026-07-19 — analyzer: pruned-edge lesson entities leave the unattributed queue)
- **The graph analyzer's "entities with no project" finding now also excludes
  entities referenced by `lessons.entity_id`/`object_entity_id`** — the
  residual tail of the 0.9.0 lesson-entity exclusion. That exclusion keyed on
  edge signal (all edges `prefers`/`avoids`), so lesson entities whose lesson
  edges were pruned by hygiene carry ZERO edges and stayed flagged (4 on the
  live bank). The service now passes the lesson-referenced id set from
  storage into the analyzer, which stays DB-free.

## [0.9.0] - 2026-07-19

### Changed (2026-07-19 — analyzer: lesson entities leave the unattributed queue)
- **The graph analyzer's "entities with no project" finding now excludes
  lesson-only entities** — task/approach nodes minted by `memory_outcome`
  whose every edge is a `prefers`/`avoids` lesson relation. They carry no
  fact traces or mentions, so the mention-scan can never attribute them;
  flagging them was permanent review-queue noise (~137 on the live bank).
  Entities that also carry normal relations still flag as before.

### Fixed (2026-07-19 — spurious extractor fallback + its visibility)
- **The sonnet shim's `/health` is stale-while-revalidate** — the actual root
  cause of the spurious fallbacks (the restart correlation was a red
  herring): on cache expiry (5-min TTL) `/health` ran a REAL `claude -p`
  completion taking seconds, while the daemon probes with a 3s timeout, so
  any dream arriving after an idle period hit the stale cache, timed out,
  and silently fell back (3/3 live dreams). A stale cache now answers
  instantly with the last verdict and refreshes in a background thread; the
  startup path warms the cache before the server accepts connections, so no
  request ever hits the blocking empty-cache branch. (Requires a shim
  restart to take effect.)
- **The auto-mode extractor probe retries once** (2s apart) before falling
  back — belt-and-braces for genuinely transient probe failures.
- **The Console's extractor chip now warns when the LAST dream ran on the
  fallback** even though the primary is healthy again — previously that state
  rendered the green "primary ✓" chip with the fallback run visible only in
  the hover tooltip, so silent degradation stayed silent.

### Changed (2026-07-19 — dream graphing: provenance stamping + typed-relation prompt)
- **Dream-minted relation entities now carry project provenance.** Entities
  CREATED while linking a dream batch's relations are stamped into
  `entity_sources` with the batch's entry sources (scopes policy applied via
  the new shared `ScopesConfig.scope_keys()`: case-fold, exclusions, umbrella
  rollup; `origin='derived'`). Relation endpoints have no fact traces, so the
  backfill could never attribute them after the fact — they were the bulk of
  the "entities with no project" review finding. The stamp also feeds the
  cross-project gate within the same batch, so a freshly minted entity from
  project A linking to a project-B entity is routed to review like any other
  cross-project claim. Follow-up fix (caught in live verification):
  `dream_pull` entry dicts now carry `source` — they didn't, so `dream_run`
  silently passed an empty source set and nothing was stamped; regression
  pinned by an end-to-end `dream_run` test.
- **The relations extraction prompt no longer invites `related-to`.** The old
  tail ("if a real connection fits none of the specific ones, use
  'related-to'") was the source of the untyped co-mention faucet the
  quarantine diverts. The prompt now demands the most specific listed
  relation, restricts `related-to` to explicitly-stated connections, and
  instructs skipping pairs that merely appear together in the same note.

### Changed (2026-07-19 — installer wires Codex through the stdio shim)
- **Shim mode now applies to both clients.** The installer's Codex branch
  registered plain HTTP unconditionally, so an installer-wired Codex was
  exactly the "second client with no identity of its own" the session-
  identity docs warn about — while a Claude Code session's hook pointer is
  fresh, Codex writes attribute to Claude's episode (tier 3). `--transport
  shim` (the default) now wires Codex as `codex mcp add pseudolife-memory
  -- pseudolife-mcp`, giving each Codex session its own tier-1
  `X-PL-Session` identity; HTTP remains the fallback when no shim tooling
  exists and the explicit `--transport http` choice. The shim install is
  memoized so `--client both` runs pipx/pip once. (In the PowerShell
  installer the shim block became a function — every native command in it
  pipes to `Out-Host`, since a PS function's return value would otherwise
  absorb pipx/pip output and make a failed install read as success at the
  call site.)

### Changed (2026-07-19 — graph hygiene round 2: scope purge, nested topics, edge quarantine)
- **`backfill_entity_sources` now purges contaminated derived scope rows** on
  every run: sources in `memory.scopes.exclude` and legacy mixed-case scope
  keys are deleted (`origin='derived'` only — manual assignments are never
  touched). Previously the backfill only upserted, so an excluded meta tag
  re-inserted once (e.g. by pre-scope-policy code) stayed a "project" forever.
  Benign stale derived rows are deliberately kept: attribution must not decay
  when retention prunes the entries it was derived from.
- **`/api/graph/projects` is rollup-aware**: a source mapped to an umbrella in
  `memory.scopes.rollup` now carries `parent` (additive field), and the
  Console's project switcher nests children under their umbrella (`↳` prefix)
  instead of rendering the family as flat peers.
- **Low-confidence dream edges are quarantined to `edge_proposals`** instead
  of the live graph: edges scoring below the new
  `memory.dream.relation_quarantine_below` (default 0.5) file a review
  proposal (`source="dream-low-confidence"`). At the default this catches
  exactly the untyped `related-to` co-mention edges (confidence 0.45), which
  were entering the live graph at ~19/day (dubious-edge findings 34 → 120 in
  four days). Typed clean edges (0.70) are unaffected; `0.0` disables the
  quarantine and restores write-live behavior.

### Fixed (2026-07-19 — concurrent test runs no longer terminate each other)
- **Two overlapping `pytest tests/` invocations produced 15–170 nondeterministic
  `psycopg.errors.AdminShutdown` failures/errors with a different victim set
  every run** — `pg_conn` (and evals' `reset_bench()`) reap every other backend
  on their database before truncating, and both runs shared one
  `pseudolife_memory_test` / `pseudolife_memory_bench`, so each run's reaper
  killed the other run's live connections. Each pytest process now provisions
  private per-run databases (`pseudolife_memory_test_<pid>`,
  `pseudolife_memory_bench_<pid>`), dropped at exit, with dead-run leftovers
  pruned on the next run (pid-liveness-checked, so live concurrent runs are
  never touched). `PSEUDOLIFE_TEST_DATABASE_URL` still wins verbatim (CI), and
  eval CLI runs keep the fixed bench name (`PSEUDOLIFE_BENCH_DB` is only pinned
  by the test suite). Regression pins in `tests/test_pg_run_isolation.py`.

### Changed (2026-07-19 — plugin no longer bundles the MCP server)
- **The Claude Code plugin is now the hooks + commands layer only**: its
  `.mcp.json` (HTTP server entry) is removed. Claude Code loads a plugin
  MCP server *alongside* any user-registered server for the same daemon —
  no deduplication, no per-server disable (only the whole-plugin toggle,
  which would also kill the identity/briefing hooks) — so plugin + installer
  users carried a doubled tool namespace in every session. The MCP transport
  is now always registered by `ops/install.*` (stdio shim by default, HTTP
  with `--transport http`) or the README's `claude mcp add` one-liner; the
  installer's plugin-detected branch still skips the hook and CLAUDE.md
  wiring but no longer skips the transport step. **Migration (existing
  plugin users):** after the plugin updates, run the installer once (or the
  one-liner) to register the transport — and if you had both before, the
  duplication disappears on its own.

### Fixed (2026-07-19 — resume-after-reap fragmentation on the hook path)
- **A SessionStart re-fire after the idle reaper no longer forks the
  episode.** Resume-on-return (reopen a recently-reaped session root instead
  of leaving a husk) lived only in the store path, so when the idle reaper
  closed a long session's root and Claude Code then re-fired SessionStart
  (source `resume`/`compact`), the hook path (`episode_start_session`) opened
  a *second* root for the same session — fragmenting one logical session
  across two episodes (and feeding the zero-signal outcome-inference scan a
  spurious candidate). Both paths now share one `_resume_closed_session_locked`
  helper, so a return via either resumes the same root within
  `PSEUDOLIFE_SESSION_RESUME_SECONDS` (default 6 h; `0` disables).

### Fixed (2026-07-19 — active-session pointer TTL)
- **A crashed client's tier-3 pointer no longer misattributes forever.** The
  machine-scoped active-session pointer (identity tier 3) is set at
  SessionStart and cleared at SessionEnd — but a client that crashes never
  fires SessionEnd, so its pointer used to attract every later handle-less
  direct-HTTP write until the next SessionStart overwrote it. The pointer now
  expires: tier-3 resolution ignores one older than
  `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (default `21600` = 6 h, matching the
  resume window; `0` disables), falling through to the transport/idle-gap
  floor. Refresh is on-set only (SessionStart, which Claude Code re-fires on
  resume/compact); a legacy pointer with no stored timestamp reads as stale
  and is ignored until re-registered, never a crash.

### Added (2026-07-19 — band-ablation offline rebuild)
- **evals**: `evals/band_ablation.py` — 8-band continuum vs single-table
  retrieval ablation as an offline context rebuild (2026-07-17 architecture
  critique). `replay` re-ingests each LongMemEval-KU question's haystack
  turns CPU-only (real `svc.store` path, dreaming skipped) and serialises
  full band state per question; `rebuild` re-ranks the rag/hybrid raw-turn
  selection from that state under two policies (`continuum` mirrors the
  CMS Pool-1 depth-modulated recency ranking plus the slot channel;
  `flat` is one pool with a single depth-0 recency term) × two timestamp
  modes (`wall` = served regime, age≈0; `hist` = session-date timestamps
  ranked from the question date, making the half-life continuum real),
  emitting four `arm1-abl-*` JSONLs ready for `replicate.py`. Sanity gate:
  the continuum+wall mirror re-selects the originally served rag context
  at 1.000 agreement across all 78 questions.

### Changed (2026-07-19 — project-scope hygiene)
- **Scope derivation policy** (`memory.scopes` config): the entity-sources
  backfill now case-folds scope keys (`Pseudolife` and `pseudolife` are one
  scope), skips meta source tags that must never become projects
  (`scopes.exclude`, default `status`/`claude`/`agent`/`correction`), and
  writes an additional umbrella scope for sources mapped in `scopes.rollup`
  (both rows kept — family view and fine-grained filter coexist). Previously
  every distinct `entries.source` string minted its own Atlas project,
  fragmenting one project family across many scopes.

### Changed (2026-07-19 — session identity contract)
- **Episodes no longer key on the transport connection.** Five-tier
  identity: shim `X-PL-Session` → explicit `episode` handle on write tools
  (advertised in the session briefing) → hook-registered session (SessionStart
  now forwards Claude Code's `session_id`; new SessionEnd hook closes the
  episode promptly) → legacy `mcp-session-id` (removed from MCP 2026-07-28,
  SEP-2567) → writer+idle-gap floor. A session can no longer close another
  session's episode (`episode_end` ownership guard). The installer now wires
  the stdio shim by default (`--transport http` to opt out) — per-session
  identity for concurrent sessions.

### Fixed (2026-07-19 — hook mutation paths honor the bearer gate)
- **`GET /api/hook/session-start` and `POST /api/hook/session-end` mutated
  state without the bearer-token check when `PSEUDOLIFE_MCP_TOKEN` is
  configured** — session-start only used `_authorized(scope)` to gate the
  briefing *content*, not the `?session_id=` episode registration / active-
  session pointer write, and session-end never checked it at all. On a
  LAN-exposed token-gated daemon this let an unauthenticated client hijack
  the active-session pointer (misattributing untagged writes) or
  force-close sessions. Fixed: session-start now drops `session_id`/`source`
  entirely when unauthorized (output stays byte-identical to the
  instructions-only response — no registration, no advertisement); session-
  end now returns 401 (same shape as `/api`'s gate) when a token is
  configured and the bearer is missing/wrong. ASGI-level regression coverage
  added in `tests/test_session_identity.py` (there was none before, in
  either direction).

### Fixed (2026-07-19 — session identity: header+handle attribution)
- **`memory_store` / `memory_fact_set` / `memory_outcome` attribution is now
  unconditional on a valid `episode` handle, even when a header session
  (`X-PL-Session` / `set_writer_context` override) is also present** — per
  the design contract's "Precedence rationale" (`docs/superpowers/specs/
  2026-07-18-session-identity-contract-design.md`), the header wins
  *identity* but the write must still attribute to the handle's episode.
  `MemoryService.store()` previously wrote its attribution override to
  `self._cms.bands[0].entries[-1]` **after** `CMS.store()` returned — but
  `CMS.store()` runs its promotion walk internally before returning, and
  promotion builds a **new** `MemoryEntry` object in the destination band
  (`CMS._consolidate`), so a promoted entry silently kept the header
  session's lazily-opened episode instead of the handle's. Fixed by adding
  `CMS.store(..., attribution_episode_id=...)`, applied to the entry
  immediately after the session-key stamp and before both the write-through
  insert and the promotion walk, so it lands in the persisted row and
  survives promotion. `record_outcome` already attributed unconditionally
  (`resolved[0] if resolved is not None else ...`, no header check) and
  needed no change; pinned with a regression test regardless.

### Removed (2026-07-18 — neural-era residue sweep)
- **`pseudolife_memory/memory/contrastive.py` deleted** — the
  `ContrastiveUpdater` / `NegativeSignalDetector` classes were constructed
  nowhere on any daemon path (the zombie sweep noted this at
  `service.py:541-543`); verified unreferenced anywhere in code, tests, or
  docs before removal. `ContrastiveConfig` (the config dataclass consumers
  actually touch) lives in `pseudolife_memory/utils/config.py` and is
  untouched.
  `pseudolife_memory/memory/context_builder.py` was checked against the same
  claim but is **not** dead — `service.py` imports its `_relative_time`
  helper and `pseudolife_memory/memory/__init__.py` re-exports
  `ContextBuilder` — so it was kept.
  **Superseded 2026-07-30** (see the `[Unreleased]` dead-code sweep): both
  claims in the two paragraphs above were wrong or have since lapsed.
  `ContrastiveConfig` had **no** consumers — deleting `contrastive.py` took
  the last one, and nothing re-read the config dataclass; it is now removed.
  `ContextBuilder` was kept on the strength of a re-export that was itself
  the only reference — the class is now removed and only `_relative_time`
  survives in `context_builder.py`.
- **`pseudolife_memory/memory/miras/retention.py` docstrings corrected** —
  the module and factory docstrings described `weight_decay` as "gradient
  shrinkage of the memory weights" applied during an update step that no
  longer exists post-v0.5. Rewritten to say plainly that `weight_decay` is
  vestigial (kept only for factory-signature parity), matching the honest
  framing `RetentionPolicy` in `protocols.py` already carried. No behavior
  changed.
  `pseudolife_memory/memory/titans_memory.py` was audited against the same
  claim: it already documents plainly that the TITANS neural memory is gone
  and the file only hosts the `MemoryEntry` / `RetrievalResult` dataclasses,
  so no edit was needed there.
  Archive pointer for all removed neural machinery: branch
  `archive/neural-memory-titans`.

### Security (2026-07-18 — mcp 1.28.1, Dependabot alert 5)
- **`mcp` bumped 1.28.0 → 1.28.1 in `ops/requirements.lock.txt`**
  (CVE-2026-59950 / GHSA-vj7q-gjh5-988w, high: the SDK's WebSocket server
  transport accepted requests without Host/Origin validation). The daemon
  serves streamable HTTP only and never mounts the WebSocket transport, so
  the vulnerable path was unreachable — taken anyway as a clean patch bump:
  no transitive pin moves, and every 1.28.1 constraint is already satisfied
  by the existing lockfile pins.

### Fixed (2026-07-18 — client-neutrality follow-ups)
- **The `agent` default source no longer hijacks derived session titles** —
  it joins `claude` in the title vote's noise set, so real project sources
  keep winning the dominant-source vote (it still wins when it's all there
  is). The service-layer `store()` default now matches the MCP surface
  (`agent`, was `claude`).
- **`docs/guide/configuration.md` writer row updated** for the
  client-selectable compose default (`mcp-client`), including the migration
  note for pre-selector installs whose tier map keys on `claude-code`.

### Added (2026-07-18 — client-neutral MCP guidance)
- **The MCP initialization now advertises the memory workflow through the
  protocol's `instructions` field** — clients that honor server instructions,
  including Codex, learn the recall/capture/outcome loop without relying on a
  Claude-specific standing-instructions file. Package and registry descriptions
  now describe MCP-compatible agents rather than a single client.
- **Default tool attribution is no longer Claude-specific** — unsourced MCP
  stores use `source="agent"`. Existing memories and explicit `source` values
  are unchanged.

### Added (2026-07-18 — Codex install parity)
- **The one-shot installers now support `claude`, `codex`, or `both` clients**
  (`--client` / `-Client`) while preserving Claude as the compatibility
  default. Preflight checks only the selected client CLI; MCP registration uses
  each client's native HTTP command; and the optional standing memory block is
  offered for Claude's `CLAUDE.md` and Codex's `AGENTS.md`.
- **The existing briefing hook now installs into Codex too** — the hook helpers
  target either `~/.claude/settings.json` or `~/.codex/hooks.json`. Both clients
  accept the same `SessionStart` `additionalContext` payload, so briefing
  generation remains a single implementation.
- **Container installs now register a briefing command that actually exists** —
  the hook runs `pseudolife-mcp briefing` inside the daemon container. The old
  one-shot flow wrote a host command without installing the host package, so
  fresh Docker-only installs silently missed every session briefing.
- **Installer-managed daemon provenance reflects the selected client** — writer
  IDs use `claude-code`, `codex`, or `mcp-client` for a shared install.

### Changed (2026-07-18 — outcome-inference abstention hardening)
- **The inference prompt now requires an attempt-with-result before
  claiming an outcome**: read-only/note-taking sessions and deferred
  decisions abstain instead of inventing signals, and the
  correction-vs-failure boundary is explicit (an approach failing on its
  own is failure; correction requires the user correcting the assistant).
  Bundled-sidecar (E4B) score on the `--infer` rung: 0.562 → 0.875 with
  both abstention fixtures passing; Sonnet primary unregressed
  (~0.90 over 3 runs). Bench matcher now grounds expected keywords
  against `task`+`about` (both schema-compliant phrasings accepted).

### Added (2026-07-18 — auto-outcome inference at episode close)
- **The daemon now infers outcome signals for silent sessions**: when a
  session episode closes with stored entries but zero `memory_outcome`
  calls (35% of real sessions, measured 2026-07-18), a new dream stage
  infers up to 3 signals (`origin="inferred"`) from the episode's own
  record — including status-source entries — and the same dream
  synthesizes lessons from them at confidence 0.4 (vs 0.6 explicit).
  Cursor + bounded retry live in `meta` (no schema change).
  `dream_status` gains an `infer_outcomes` block and `would_fire` counts
  pending inference. Kill switch: `memory.lessons.infer_outcomes: false`.

### Changed (2026-07-18 — Arm-1 deploy evidence downgraded by replication)
- **First replicated LongMemEval-KU comparison** (5 replicates/config,
  paired permutation test, n=78): the shipped Arm-1 fine-tuned extractor's
  cortex gain is +0.080 at **p = 0.17** (pre-registered threshold 0.05 —
  not confirmed; the single-run "+0.102" was inflated), and the hybrid arm
  shows no measurable benefit (p = 0.83). Default stays (point estimate
  positive, no evidence of harm) but is flagged for revisit. The untagged
  `qwen-27b` 0.705 headline run predates context persistence and is
  unreplicable; its replicable sibling (`w0`) scores hybrid 0.695 ± 0.017.
  Docs renumbered accordingly (`docs/guide/benchmarks.md`,
  `evals/README.md`).

### Added (2026-07-18 — eval replication layer + regression gate)
- **evals**: `evals/replicate.py` — answer-phase replication over
  `longmemeval_bench.py` (`spawn`/`run`/`agg`/`compare`/`gate-check`/
  `baseline`): stripped `-rN` replicate files, mean±std aggregation to
  `.agg.json`, paired permutation compare. Measured motivation: identical
  configs vary ~7.7 pp cortex accuracy run-to-run (judge-side noise), wider
  than several previously-published single-run deltas.
- **evals**: `evals/regression_gate.ps1` (pinned replicated `arm1-gate`
  slice vs committed `regression_gate.baseline.json`; covers
  retrieval/serving/judging — the ladder still covers extraction) and
  `evals/overnight_replicates.ps1` (Arm-1 re-verification, pre-registered
  p < 0.05 rule).
- **docs**: variance/replication methodology in `evals/README.md`; honesty
  note in `docs/guide/benchmarks.md` (single-run numbers carry a ~7.7 pp
  noise band).

### Fixed (2026-07-16 — deterministic `recent` ordering on same-tick stores)
- **`recent` now tie-breaks entries whose wall-clock timestamps collide**
  within one `time.time()` tick. Each `MemoryEntry` carries a transient
  process-monotonic creation sequence (`seq` — not persisted; hydration
  re-stamps in insertion order), preserved across band promotion, and
  `recent` sorts by `(timestamp, seq)` descending. Previously same-tick
  stores listed in effectively arbitrary order (stable sort + promotion
  relocations), which flaked `recent`-ordering assertions under load.

### Added (2026-07-16 — Console loop-health tile)
- **Observatory "Loop health" panel**: windowed (7d vs prior 7d) stores and
  outcome-signal counts with trend arrows and a success/failure/correction
  breakdown, session count, per-session rates, pending signals, and
  last-lesson recency — the measurement side of the memory-loop
  instructions. Head chip flags "no outcomes logged" when the REFLECT beat
  isn't firing. Data: new `loop_health` on `PostgresStorage` (indexed
  COUNT queries; consumed signals still count — retention is the only
  eraser) wrapped by `MemoryService.loop_health` (`{"available": false}`
  without Postgres), surfaced as `loop` in `/api/overview`.

### Added (2026-07-16 — cold-bank onboarding in the session-start hook)
- `/api/hook/session-start` appends a short seeding guide when the bank is
  provably empty (`total_memories == 0`): name the session, store a few
  durable facts, set one canonical value, log the first outcome. Warm
  banks, stats failures, and unauthorized token-gated requests never see
  it — onboarding noise on a working bank is worse than none on a cold one.

### Changed (2026-07-16 — richer memory-loop instructions + per-user override)
- **The served memory-loop instructions are now the full three-beat loop**
  (RECALL / CAPTURE / REFLECT with the hard-won operational details: the
  `fact_get` null-slot footgun, `superseded_by_text` handling, session
  titling, sub-episodes, `source="status"` routing, the surprise-gate note,
  a new *never store secrets* rule, and a `memory_recall` pointer for
  multi-hop questions). Applies to both the plugin-served context
  (`MEMORY_LOOP_BLOCK`) and `examples/CLAUDE.memory.md` — guard-kept
  identical. Roughly 650 tokens per session, up from ~300.
- **`<data_dir>/hook-instructions.md` override**: when present and
  non-blank, `/api/hook/session-start` serves it instead of the shipped
  block (briefing still appended) — customize the standing instructions
  without forking the plugin; blank or unreadable falls back to the default.

### Added (2026-07-16 — Claude Code plugin + in-repo marketplace)
- **Claude Code plugin `pseudolife-memory`** (`plugin/`), installable from
  this repo as a marketplace (`/plugin marketplace add
  Pseudogiant-xr/Pseudolife-MCP` → `/plugin install
  pseudolife-memory@pseudolife-mcp`). It replaces the installer's three
  wiring steps: a bundled `.mcp.json` (HTTP server at `127.0.0.1:8765/mcp`)
  instead of `claude mcp add`, a SessionStart hook (bash + curl, no pip
  package on the host) instead of the settings.json briefing hook, and the
  memory-loop instructions served as session context instead of the
  CLAUDE.md append. Ships `/dream` and a new `/memory-status` command.
  Spec: `docs/superpowers/specs/2026-07-16-claude-code-plugin-design.md`.
- **New daemon endpoint `GET /api/hook/session-start`** (plain text, 200
  always): the memory-loop instruction block plus the session briefing.
  Loopback-gated like the rest of `/api`; with a token set, an
  unauthorized request gets the instructions only — never memory content.
  Response capped at 9,500 chars (Claude Code's 10k hook-stdout limit).
- **Installers skip wiring when the plugin is installed** (detected via
  `~/.claude/plugins/installed_plugins.json`) so hook + CLAUDE.md + mcp-add
  don't double up.
- Guard tests (`tests/test_plugin_packaging.py`): memory-loop block ==
  `examples/CLAUDE.memory.md`, plugin `/dream` == `examples/commands/dream.md`,
  plugin.json version == pyproject version (the release version-cut now
  touches **five** files), manifest/hook wiring sanity.

### Changed (2026-07-16 — sonnet-only leads the extractor choice)
- **Installer menu reordered**: the interactive extractor prompt in
  `ops/install.sh` / `ops/install.ps1` now offers `1) sonnet-only`,
  `2) sonnet-fallback`, `3) sidecar` (was sidecar-first), framing
  sonnet-only as the lightest install — the ~9 GB sidecar image is never
  built or pulled. **The number→mode mapping changed**: pressing `1` now
  selects sonnet-only, not sidecar. Non-interactive `--extractor <mode>`
  flags are unaffected. The README Quickstart bullets and its
  non-interactive example were reordered to match.

### Security (2026-07-16 — Dependabot triage: all four alerts unreachable, no bump possible)
- Triaged the four Dependabot alerts on `ops/requirements.lock.txt` (the
  daemon-image lock; none affect `pyproject.toml` install floors). All four
  were dismissed as *vulnerable code not used*, with the reasoning recorded
  in the lockfile header:
  - **chromadb CVE-2026-45829** (critical, pre-auth code injection in the
    Chroma HTTP server API): no patched release exists — 1.5.9 is both the
    locked and the latest version. The daemon embeds
    `chromadb.PersistentClient` with fixed collection parameters and never
    runs the Chroma server, so the vulnerable endpoint is not exposed. Bump
    when a patched release lands.
  - **transformers CVE-2026-1839 / CVE-2026-4372 / CVE-2026-5241** (Trainer
    checkpoint load, malicious-config RCE, LightGlue `trust_remote_code`
    bypass): fixed only in 5.x, but the image's ONNX embedding backend
    (`optimum-onnx`, latest 0.1.0) caps `transformers<4.58`, and 4.57.6 is
    the last 4.x release — no backport exists. All three require loading
    attacker-controlled models or checkpoints; the image runs
    `HF_HUB_OFFLINE=1` with pinned weights baked at build time, so none of
    the paths are reachable. Bump to ≥5.5 once optimum-onnx supports
    transformers 5.x.

### Changed (2026-07-16 — README restructured into a front door + docs/guide)
- **README.md cut from ~1450 to ~540 lines**: it keeps the front-door
  material (badges, hook, Quickstart, tools table, install, wiring, basic
  usage patterns, capabilities, troubleshooting) and links out for depth;
  the deep material moved to six new user-facing pages under
  **`docs/guide/`** — `configuration.md`, `retrieval.md`, `dreaming.md`,
  `episodes.md`, `memory-model.md`, `benchmarks.md`. The README doubles as
  the PyPI description, so the shorter version ships at the next release.
  Guard tests moved with their content (`tests/test_release_ux.py`): the
  schema-version DSN-row pin now points at `docs/guide/configuration.md`,
  the no-test-count rule sweeps every guide page, and two new guards pin
  the MCP-registry `mcp-name` marker in the README and require every
  `docs/guide/` page to be linked from the README (nothing moved goes
  dark). `docs/README.md` now distinguishes the maintained `guide/` pages
  from internal design history.

## [0.8.1] — 2026-07-16 — Pseudolife (one word), release automation, CLI help

### Changed (2026-07-16 — the name is a word now)
- **"PseudoLife" → "Pseudolife" everywhere** (87 files): the name now reads
  as a coined closed compound — like *pseudocode*, *pseudonym* — rather than
  a CamelCase brand; machine identifiers (`pseudolife-mcp`, volumes,
  containers, env vars) were already lowercase and are unchanged. This
  includes the MCP server display name (`FastMCP("Pseudolife Memory")`), the
  Cortex Console title, and repo URLs (now matching the GitHub repo's actual
  casing; old-casing URLs redirect regardless).

### Added (2026-07-16 — PyPI Trusted Publishing workflow)
- **`.github/workflows/release.yml`** — publishing a GitHub release now
  builds + `twine check`s and uploads to PyPI via Trusted Publishing (OIDC;
  no stored API token), gated on a tag==pyproject version guard and the
  `pypi` environment's required-reviewer approval. One-time setup: add the
  Trusted Publisher on PyPI (owner/repo/workflow/environment) and create the
  `pypi` environment with a required reviewer, then retire the API token.

### Changed (2026-07-16 — WSL memory guidance matches the E4B sidecar)
- **`ops/wslconfig.example` sizing was still E2B-era** (stack "~2 GiB
  resident", 16 GB laptop → `memory=6GB`): the default E4B v2 bake mmaps a
  ~5.3 GB model, so the guidance now plans ~6–7 GiB for sidecar mode
  (16 GB → `8GB`, 32 GB → `10GB`) and notes sonnet-only installs need ~1 GiB.
  README's WSL note updated to match.

### Added (2026-07-16 — CLI help)
- **`pseudolife-mcp --help` / `-h` / `help`** prints a usage summary of all
  six modes and exits 0; unknown modes now point at `--help`. Previously the
  first thing a fresh `pip install` user typed answered "unknown mode".

### Fixed (2026-07-16 — toolset-tier follow-ups)
- **`memory_toolset` docstring** no longer claims hidden tools remain
  callable by exact name — Claude harnesses gate tool calls client-side
  against their own list, so clients must expand first. The tiers spec's
  failure-modes section records the confirming incident.
- **Every ops script requires PowerShell 7** (`#Requires -Version 7`,
  previously only the installers had it; now also update, backup, restore,
  preflight, prune-rollbacks, install-autostart) — Windows PowerShell 5.1
  turns benign native stderr (e.g. docker inspecting a not-yet-built image
  tag) into a terminating error mid-deploy; 5.1 now refuses upfront with a
  clear version error instead.

## [0.8.0] — 2026-07-16 — public-release cut

Everything since the 0.7.0 cut: the whole-bank galaxy console work, the
one-shot installer with extractor choice, Linux install parity, the published
E4B v2 extractor fine-tune as the shipped default, rollback-tag and backup
retention, the v2 release audit (state-volume backups, install hardening,
disk-growth caps), and the toolset-tier visibility rework.

### Fixed (2026-07-16 — 0.8.0.post1, metadata only)
- **PyPI `0.8.0.post1`** — identical code to 0.8.0; corrects the `mcp-name`
  ownership marker's namespace casing (`io.github.Pseudogiant-xr/…` — the MCP
  registry matches the GitHub username case-sensitively) and adds
  `server.json`, the registry manifest. Published to the official MCP
  registry as `io.github.Pseudogiant-xr/pseudolife-mcp`.

### Fixed (2026-07-16 — PyPI packaging)
- **The wheel now ships the Cortex Console** — `[tool.setuptools.package-data]`
  carries `pseudolife_memory/web/static/**` (css/fonts/js/index.html); without
  it a pip install served a 404 console (the Docker image was unaffected — it
  copies the whole package tree).
- `[project.urls]` (Homepage/Repository/Changelog/Issues) for the PyPI
  sidebar; the MCP registry ownership marker
  (`mcp-name: io.github.pseudogiant-xr/pseudolife-mcp`) embedded in the
  README, which is the package description; the README screenshot switched
  to an absolute URL so it renders off-repo.

### Fixed (2026-07-16 — tier changes now reach shim clients)
- **Shim forwards `tools/list_changed`** (landed inside the release-cut
  commit) — the shim's per-call upstream design meant the daemon's
  tier-change notification died on an ephemeral session and never reached
  the real client, which then rejected the newly visible tools client-side
  ("No such tool available" — Claude harnesses gate calls against their own
  list, so "hidden tools stay callable" never held in practice). The shim
  now advertises `tools.listChanged` in its downstream capabilities and
  re-emits the notification whenever a `memory_toolset` call reports
  `changed: true`. Found via the daily morning-brief scheduled task, which
  lost `memory_world_search`/`set` when the 2026-07-11 tier map demoted its
  writer (`claude-desktop`) to minimal.

### Fixed (2026-07-15 — Atlas polish)
- **"Assign a project" is a combobox** — the modal suggests every existing
  project (native datalist) while still accepting a brand-new name, so
  near-duplicate scopes ("Pseudolife" vs "pseudolife") stop being minted by
  accident.
- **"hide orphans" toolbar toggle** — hides degree-0 entities (the sparse
  halo unlocked by the whole-bank cap raise) in both galaxy and table views.
  Visibility-only: the layout never recomputes; the button shows the live
  orphan count.
- **Whole-bank galaxy** — the seedless graph cap defaults to 2000 nodes
  (was 300, a guard for the retired 2D canvas's O(n²) per-frame sim). The
  full ~1.2k-entity bank now renders, orphans included; the cap machinery
  and truncation banner remain as the safety valve for much larger banks.
- **No more self-moving camera** — selecting a star during the initial
  layout warmup froze the view target correctly, but the settle-time "drift
  correction" re-flew the camera (a visible zoom-out-zoom-in). Fly-to now
  freezes the simulation instead (the inspected star cannot drift), auto-fit
  runs only on a completely untouched view, and the layout pre-simulates 60
  warmup ticks so an early freeze still lands on a formed map.
- **Galaxy pulse is a decision signal again** — stars pulse only for
  item-level findings awaiting judgment (duplicates, merge/junk candidates,
  proposed links). Bulk hygiene lists (orphan / unattributed / dubious-edge —
  1,700+ names on the live bank) no longer light up the whole galaxy; they
  stay in the Review drawer.
- **Clicking a pulsing star now shows WHY it pulses**: the shell injects the
  cached review findings for that entity into its wiki-page banner (deduped
  against server-side flags), with the drawer's own confirm-gated actions —
  duplicate pairs get Merge / Mark distinct in place.

### Added (2026-07-15 — Atlas stage 3: time, review, focus)
- Galaxy time scrubber: replay the bank's growth — stars/edges appear by
  `created_at`/`asserted_at` over a fixed layout (visibility only, never
  re-simulated), with play/pause and a date readout; auto-play disabled under
  reduced motion.
- Contextual review: entities named in open findings pulse in the galaxy
  (static warn tint under reduced motion); wiki-page flag banners carry the
  same confirm-gated actions as the review drawer (merge / reject / accept /
  assign — identical descriptors, one action path); acting refreshes the
  galaxy and reopens the page.
- Isolate toggle on wiki pages: dims everything beyond the entity's 2-hop
  neighborhood client-side (no fetch) — the explore-mode replacement.

### Added (2026-07-15 — Atlas stage 2: the galaxy is the map)
- Console Graph tab rebuilt around the 3D galaxy: memory-state encodings
  (size = connections + facts, hue = project/community, brightness = recency),
  community nebulae with constellation labels, proximity-faded star labels
  (nearest 40), search with dim/highlight + Enter-to-fly, and wiki-page
  click-through — clicking a star or a wikilink flies the camera and opens the
  page. Review queue lives in a left drawer; table mode is the fallback
  (automatic, with a notice, when WebGL/the 3D bundle is unavailable).
- `/api/graph` whole-graph payload carries `created_at` per node and
  `asserted_at` per edge (additive; feeds the recency encoding now, the time
  scrubber in stage 3).
- Vendored `galaxy.bundle.js` (3d-force-graph 1.73.6 + three 0.185.1 from one
  dependency graph — single three instance; esbuild, legal comments embedded;
  license audit MIT×18/ISC×14/BSD-3×4, allowlist-gated, inventory + reproducible
  build recipe in `vendor/README.md`) replaces `3d-force-graph.bundle.js`.

### Removed (2026-07-15 — Atlas stage 2)
- The 2D canvas force-graph map and the Overview/Explore mode split. Legacy
  `mode=`/`depth=` deep links still resolve (extra params ignored); an
  `entity=` deep link overrides a sticky table view.

### Added (2026-07-15 — Atlas stage 1: entity wiki pages)
- **`GET /api/wiki?entity=X`** — one-call, live-rendered entity page for the
  console: identity + aliases + project attribution + first-seen, canonical
  facts (with lazy supersession history), cited world facts, relations in/out
  (derived edges marked with rule provenance), provenance mentions, a merged
  newest-first timeline, and open review flags (proposals + unattributed;
  never re-runs the full review scan). No LLM in the loop — pages are
  assembled from structured data at request time, so they cannot go stale.
- **Console: clicking a graph node opens the entity's wiki page** in a
  browsable panel (replaces the small node-panel); entity names inside the
  page are wikilinks that swap the page in place.
- Storage: `find_entity`/`load_graph` expose `entities.created_at`
  (additive; feeds first-seen and the upcoming galaxy time scrubber).

### Added (2026-07-14 — state-volume backup/restore, v2 release audit)
- **`ops/backup.ps1|.sh` now also tar the daemon state volume** into a
  sibling `pseudolife_state-<stamp>.tgz`: ingested `document_ingest` files
  (ChromaDB), the cortex snapshot, and graph snapshots live *only* there, so
  the pg_dump alone silently lost them on disaster recovery despite the
  compose header telling users a backup covers "state". Warn-never-throw (a
  stopped daemon skips the tar loudly but never aborts a deploy's backup
  step); rotation and the off-disk mirror cover both artifact kinds
  (`-MirrorKeep`/`--mirror-keep` caps each kind at newest N).
- **`ops/restore.ps1 -StateArchive <tgz>` / `restore.sh --state-archive`** —
  opt-in state-volume restore (a DB-only restore must not clobber current
  state): rehearsal integrity-lists the tar; `-Apply` replaces `/data` while
  the daemon is stopped, via the daemon's own image with `--volumes-from`
  (no volume-name resolution, no image pull). Without the flag, restore
  prints a hint when state archives exist.

### Fixed (2026-07-14 — v2 release audit)
- **Restore rehearsal no longer false-fails on a young bank** — the
  entries/facts sanity check now alarms only when the *live* bank has rows
  the restored copy lost; a fresh install with no dream-run facts (0 rows)
  produced a valid backup that the rehearsal declared untrustworthy.
- **`ops/install.sh` no longer aborts mid-install when shim autostart fails**
  (parity with the `.ps1`): on a host without `systemd --user` (macOS, some
  WSL), a Sonnet-mode install died between `compose up` and the
  hooks/`claude mcp add`/health steps — a running stack never wired into
  Claude Code. Step 7 is now best-effort with a re-run hint.
- **`ops/install.ps1` and `ops/install-hook.ps1` now enforce
  `#Requires -Version 7`** — under Windows PowerShell 5.1 their
  `Set-Content -Encoding utf8` writes a BOM that garbles the first `ops/.env`
  key and can break `settings.json` parsing; 5.1 users now fail fast with the
  pwsh install pointer instead of writing corrupted config.
- **`encode([])` returns an empty `(0, dim)` tensor again** — the 2026-07-12
  embedding-cache rework made `torch.stack` raise on an empty text list
  (latent; no current caller can pass one, guard restored for defensiveness).

### Changed (2026-07-14 — v2 release audit: disk hygiene + preflight)
- **All three compose services now cap container logs** (`json-file`,
  `max-size: 10m` × 3 files) — Docker's default json-file driver has no size
  limit, so months of daemon/pg/extractor stdout grew the disk unbounded.
  Applies on the next `up -d` (container recreate).
- **`.dockerignore` now excludes `evals/`, `tests/`, `examples/`, and
  `unsloth_compiled_cache/`** — no Dockerfile copies from them, and a
  populated `evals/models/` (tens of GB of local GGUFs) was being tarred
  into every `docker compose build` context and build-cache entry.
- **`ops/preflight.ps1|.sh` now check ports 8765/5433** (warn-only): a taken
  port previously surfaced as a cryptic compose "port is already allocated";
  ports held by an existing Pseudolife stack count as OK, so idempotent
  re-runs stay green.
### Added (2026-07-14 — superseded-row compaction)
- **Superseded-row compaction** — corrections no longer grow the canonical
  stores forever: on each dream sweep tick, `facts`/`world_facts`/`lessons`
  keep the newest 3 superseded/retired versions per slot and purge older
  ones after 30 days (config `memory.compaction.*`, console group
  "Retention"; the per-slot sync deletes the PG rows). `memory_history`
  timelines keep their recent versions; entries (bounded band eviction,
  supersession is retrieval-load-bearing) and edges (sticky-removal
  tombstones the dream's `revive=False` depends on) are deliberately
  untouched. The in-memory cortex supersession log is now capped at its
  persisted size (200). No schema change. Spec:
  `docs/superpowers/specs/2026-07-14-superseded-row-compaction-design.md`.

### Changed (2026-07-14 — extractor default = published v2 fine-tune)
- **`ops/Dockerfile.extractor` now bakes the bespoke extractor fine-tune by
  default** (`MODEL_URL` →
  `Pseudogiant-xr/pseudolife-extractor-gemma-4-e4b/…/pseudolife-extractor-e4b-v2-Q4_K_M.gguf`
  on Hugging Face, ~5.3 GB) instead of the Gemma 4 E4B QAT base model. This is
  the Arm-1 registry-datagen student (same-session KU-oracle: cortex 0.705 vs
  0.603, hybrid 0.756 vs 0.744 against the prior fine-tune; ladder stale-leak
  0.0) — fresh installs now get the production extractor without a local GGUF
  mount. The lighter E2B QAT bake for constrained machines is unchanged
  (`--build-arg MODEL_URL=…`), as is the runtime GGUF mount override.

### Added (2026-07-14 — rollback-tag retention in the deploy scripts)
- **`ops/prune-rollbacks.ps1` / `ops/prune-rollbacks.sh`**, called by
  `update.ps1` / `update.sh` after tagging each new rollback image (and safe
  to run standalone): keeps the newest N `pre-*` rollback tags of the daemon
  image and `docker rmi`s the rest — one tag was minted per deploy and never
  garbage-collected, which had piled up ~60 stale tags inside a 177GB
  docker_data.vhdx by 2026-07-14. N defaults to 2 and is overridable via
  `update.ps1 -KeepRollbacks N` / `update.sh --keep-rollbacks N`. Never
  removes the deployed tag (it doesn't match the `pre-*` naming) or any image
  a running container uses (protects the just-minted rollback mid-deploy
  too), and never touches volumes; a retention failure warns but does not
  abort the deploy.
- **Count-based mirror retention in `ops/backup.ps1|.sh`** — `-MirrorKeep N`
  / `--mirror-keep N` / `PSEUDOLIFE_BACKUP_MIRROR_KEEP=N` caps the off-disk
  mirror at the newest N files by filename stamp (cloud-synced folders have
  untrustworthy mtimes and metered space; the age-based `KeepDays` window
  kept 10+ files there with no way to say "exactly N"). Unset/0 preserves
  the age-based behavior; the primary `data/backups` rotation is unchanged.

### Added (2026-07-14 — one-shot installer with extractor choice, #13 tier 2)
- **`ops/install.sh` / `ops/install.ps1`** — idempotent one-command install:
  preflight → volumes → **extractor choice** → compose up → session hooks →
  CLAUDE.md memory block (consent prompt; `--claude-md append|skip`) →
  `claude mcp add` → health check → per-mode verify hints. The extractor mode
  is always an explicit choice (`--extractor sidecar|sonnet-fallback|
  sonnet-only`; interactive prompt, no default); re-running with a different
  mode is the supported way to switch. Dream env lives in a managed marker
  block in `ops/.env`, so user lines outside it survive re-runs.
- **The extractor sidecar is now optional** (`sonnet-only` mode): the
  installer writes `profiles: ["disabled"]` for the sidecar into the
  gitignored compose override, so the ~9 GB image is never built or pulled;
  the mode sets `PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary` (states the intent,
  keeps the auto-without-fallback startup warning silent) and dreams pause
  with per-sweep retry while the shim is down. A pre-existing user override
  (e.g. a GGUF mount) is never merged into — the snippet is printed instead.
  Spec: `docs/superpowers/specs/2026-07-14-installer-extractor-choice-design.md`.

### Changed (2026-07-14 — one-shot installer with extractor choice, #13 tier 2)
- **The daemon no longer `depends_on` the extractor sidecar** — extraction is
  runtime HTTP with per-sweep retry, and a hard dependency on a
  profile-disabled service is a compose error. Worst case for stock installs:
  a dream sweep fires before the extractor finishes loading → that probe
  fails and retries next sweep.

### Fixed (2026-07-14 — hook installer + shim autostart, found by installer live-test)
- **`ops/install-hook.ps1|.sh` no longer install the obsolete episode-start/
  episode-end hooks** (obsolete since the 2026-06-30 session-scoped episodes
  rework — the daemon owns episode lifecycle, keyed by `mcp-session-id`) and
  now **remove** any found, so installs that got them earlier converge.
- **`ops/install-shim-autostart.ps1` no longer claims success when
  `Register-ScheduledTask` is denied** (CIM cmdlet errors don't reliably
  terminate under `$ErrorActionPreference = "Stop"`): the cmdlet gets an
  explicit `-ErrorAction Stop` plus a Get-ScheduledTask existence check, so
  an unelevated run now throws and `install.ps1` prints the recovery steps.

### Added (2026-07-14 — Linux install parity + install UX, issues #11/#12/#13)
- **`ops/install-shim-autostart.sh`** — Linux parity for the Sonnet-shim
  autostart (`systemd --user` unit; the `.ps1` was Windows-only, so a Linux
  user following the README silently stayed on the sidecar). Binds the docker
  bridge IP, not loopback: `host-gateway` routes container→host traffic to
  the bridge, where a `127.0.0.1` bind is invisible. `sonnet_shim.py` grew a
  `--host` flag (default `127.0.0.1`) to support that.
- **`ops/preflight.sh` / `ops/preflight.ps1`** — check-only doctor scripts
  (issue #13 first slice): verify docker (installed / daemon reachable /
  socket permission), compose v2, git, python, and the `claude` CLI, printing
  the exact remediation line per failure. Never installs anything.
- **Startup warnings for silent extractor half-configurations**
  (`startup_extractor_warnings` in `dream.py`, logged by the daemon at dream
  sweep start): unresolvable `host.docker.internal` (missing `extra_hosts`),
  `extractor_mode=auto` with a host-side primary but no fallback (auto is
  inert), and primary == fallback (the intended primary is never used). The
  stock single-extractor default stays silent.

### Changed (2026-07-14 — Linux install parity + install UX, issues #11/#12/#13)
- **The CLAUDE.md memory-loop block is now a first-class install step** — the
  Quickstart appends `examples/CLAUDE.memory.md` to `~/.claude/CLAUDE.md`
  explicitly, the agent-setup section leads with the same one-liners, and
  `ops/install-hook.ps1|.sh` warn (check-and-advise only, never editing
  CLAUDE.md) when the settings-adjacent CLAUDE.md lacks the block. A fresh
  install on another machine (#12) ended with healthy hooks + daemon but a
  memory loop that never fired, because no standing instruction existed and
  the README only carried it in a deep section.
- **`extra_hosts: host.docker.internal:host-gateway` is now enabled by
  default** in `ops/docker-compose.yml` (was a commented snippet) — on Linux
  Docker Engine, any host-side extractor URL silently failed every probe and
  dreams stayed on the fallback forever; the entry is harmless on Docker
  Desktop.
- **`ops/update.ps1|.sh` scaffold `ops/.env`** from `ops/.env.example` when
  missing (all values commented — behavior unchanged, knobs discoverable).
- README: preflight + Linux docker-group prerequisite in the Quickstart, and
  the Sonnet-primary section gained the Linux autostart path, the
  "both vars flip together" warning, and a `memory_dream(action="status")`
  verify step.

### Changed (2026-07-12 — release hygiene: machine-local compose overrides)
- **Extractor GGUF swaps moved to `ops/docker-compose.override.yml`**
  (gitignored) — the tracked compose file no longer mounts a fine-tuned GGUF
  that only exists on the maintainer's machine (a fresh clone got an empty
  directory bind-mounted over `/models/extractor.gguf` and the sidecar
  crash-looped; the baked base model now serves by default).
  `ops/update.ps1|.sh` add the override automatically when the file exists
  (explicit `-f` disables compose's auto-merge); manual compose commands
  append `-f ops/docker-compose.override.yml`.
- **`ops/.env.example`** — a commented template for every env override the
  compose stack reads (volume names, `POSTGRES_PASSWORD`, `TZ`, toolset tier
  map, dream extractor primary/fallback endpoints).
- Dev harnesses and docs no longer hardcode maintainer-machine paths or LAN
  endpoints: eval scripts derive from `$env:USERPROFILE`/`Path.home()`/
  `$PSScriptRoot`, `sonnet_shim.py` resolves the `claude` CLI from `PATH`
  (`PSEUDOLIFE_SHIM_CLAUDE_CLI` or `--cli` overrides), and the `qwen-a3b`
  bench rung reads `PSEUDOLIFE_BENCH_A3B_URL` (default localhost) like the
  existing `PSEUDOLIFE_BENCH_QWEN_URL`.

### Added (2026-07-12 — embedding backend + cache, reranker margin gate)
- **ONNX embedding backend** (`embedding.backend: onnx`, new `[onnx]` extra =
  `optimum[onnxruntime]`) — the same MiniLM through onnxruntime at ~3x the
  single-text encode speed on CPU (5.0ms → 1.6ms measured) with **bit-identical
  embeddings** (fp32 cosine vs torch = 1.00000; the qint8 variants were
  rejected for ~0.008 cosine drift). Fail-soft: any load failure falls back to
  torch with a warning. Under `HF_HUB_OFFLINE=1` (the daemon's runtime
  contract) the pipeline resolves the model to its local HF snapshot first —
  optimum otherwise calls the hub tree API even when fully cached. The MCP
  defaults overlay flips `backend` to `onnx` whenever optimum is importable
  (the daemon image now bakes it + the ONNX weights); plain pip installs stay
  on torch. `embedding.backend` in config.yaml overrides either way.
  Lock note: `optimum-onnx` (all releases) caps `transformers<4.58`, so the
  image pins step back to `transformers==4.57.6` + `huggingface_hub==0.36.2`
  — the exact stack the full suite (1022 tests) validated locally. Revisit
  when optimum-onnx supports transformers 5.x.
- **Embedding LRU cache** (`embedding.cache_size`, default 1024, 0 disables) —
  keyed on `(text, normalize)`; repeat encodes of the same string (query text
  re-embedded across search + slot ops, dedup keys, warmup probes) skip the
  model forward entirely. Returned tensors are always fresh copies, so caller
  mutation can't poison the cache. Hit/miss counters on the pipeline.
- **Reranker margin gate** (`reranker.skip_margin`, default 0.0 = off) — when
  set, the cross-encoder pass (~200ms) is skipped iff the gap between the two
  best bi-encoder-adjusted scores is >= the margin (measured on *sorted* head
  scores — the head is neural + reference concatenated, not globally sorted);
  a one-candidate head is trivially unambiguous. Skips are visible in the
  retrieval trace as `reranker.reason = "unambiguous_margin"`. Opt-in: no
  deployment default until a threshold is tuned against real traffic.

### Changed (2026-07-12 — retrieval + graph lookup performance)
- **Slot-query pool (Pool 1.5) inverted index** — `memory_search` no longer
  scans every band entry per `query_text` query; slot tokens live in a
  token → (ordinal, band, entry) index. Stores extend it in place (slotless
  stores — the common case — leave it untouched), so interleaved store/search
  traffic never pays a full rebuild; removals (evict / delete / promote /
  clear) and wholesale replacement (`load` / `hydrate_cms`) flag a lazy
  rebuild. Band filtering keys on the containing band (matching the old
  full-scan semantics even for stale `bank` stamps after a preset change),
  and equal-score ties keep deterministic band-then-insertion order across
  processes.
- **`edges(dst_id)` index (schema v21 → v22)** — dst-side edge lookups
  (`merge_entity`'s dedup/repoint, reverse traversals) stop sequential
  scanning; the `UNIQUE(src_id, relation, dst_id)` constraint index only
  serves src-leading queries.

### Added (2026-07-11 — Sonnet extractor sidecar cutover)
- Dream extractor primary/fallback selection: `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`
  / `_FALLBACK_MODEL` / `_EXTRACTOR_MODE` (auto|primary|fallback), automatic
  fallback when the primary probe fails, extractor badge + override in the
  Console, `dream_status` extractor fields, shim `/health` CLI check,
  `ops/install-shim-autostart.ps1`. Inert until the fallback URL is set.

### Added (2026-07-11 — session-scoped toolset tiers)
- **Three visibility tiers** (`minimal` ⊂ `core` ⊂ `full`) filtered per
  session at `tools/list`; all tools always register and hidden tools stay
  callable (core mode previously *unregistered* them — calls now succeed).
- **`memory_toolset(action)`** — expand/collapse THIS session's tier one
  rung at a time (floor = the session's default), `status` for the ladder;
  emits `tools/list_changed` (capability now advertised).
- **`PSEUDOLIFE_MCP_TIER_MAP`** — per-writer default tiers
  (`claude-desktop:minimal,claude-code:core`); `PSEUDOLIFE_MCP_TOOLSET`
  becomes the default tier rather than a registration gate.
- **Docstring trim + manifest budgets** — per-tier char caps pinned by
  tests (minimal ≤4.5k, core ≤9.5k, full ≤15.5k; per-tool 1.6k).

### Added (2026-07-11 — entity hygiene guards)
- Entity hygiene guards (2026-07-11 curation follow-up): slot-key names fold
  to their fact's owner entity; new junk classes `metric-reading`,
  `list-artifact` (write gate + detection) and `compound-artifact`
  (detection-only); variant-token (size/quant/version) conflicts hard-block
  merge proposals in write-dedup, the dream-alias post-pass, and deep-dream
  partition; cross-project dream proposals now require a typed relation;
  REST entity-verdict routes accept `decided_by`.

### Changed (2026-07-10 — toolset tier overridable per deployment)
- **`PSEUDOLIFE_MCP_TOOLSET` in `ops/docker-compose.yml` now reads from the
  environment** (`${PSEUDOLIFE_MCP_TOOLSET:-core}`): the shipped default stays
  `core`, but a deployment can set `PSEUDOLIFE_MCP_TOOLSET=full` in `ops/.env`
  without editing the compose file. Useful for clients that defer MCP tool
  schemas client-side (e.g. Claude Code), where the full surface costs only
  the extra tool names until a schema is actually requested.

### Changed (2026-07-10 — compact-by-default recall payloads)
- **The five recall-path tools return compact entries by default** —
  `memory_search`, `memory_recall`, `memory_recent`, `memory_world_search`,
  and `memory_lesson_search` now ship only the fields an agent acts on
  (associative entries: `{id, text, source, tags, score}` plus `superseded` /
  `superseded_by_text` when set; recall facts: `{attribute, value}`, edges:
  `{src, relation, dst}`; world/lesson entries similarly trimmed to their
  documented cores, keeping `effective_confidence`/`stale`/citation and
  `re_verify`). Trims ~40% of a typical associative entry (measured on a
  representative long-text entry; short entries save proportionally more).
  New `verbose=true` flag on all five restores the full
  metadata (timestamps, counters, band/episode attribution, provenance);
  `explain=true` on `memory_search` implies `verbose`. Result payloads are
  the second half of the 2026-07-10 token-cost lever (the toolset gate below
  was the first). Cortex Console REST responses are unaffected — the
  compaction lives at the MCP transport layer only.

### Changed (2026-07-10 — core toolset promoted to the deployed default)
- **`memory_episode_start` / `memory_episode_end` are core-tier now** — the
  recommended CLAUDE.md workflow opens named sub-episodes for multi-step
  tasks, so core mode (19 tools) keeps every tool name that workflow
  references.
- **`ops/docker-compose.yml` ships with `PSEUDOLIFE_MCP_TOOLSET: core`
  enabled** (was a commented-out opt-in). The full manifest is agent context
  re-read every turn (~15k chars of descriptions across 32 tools); core trims
  ~40% of that with no workflow loss — the trimmed tail (dream, graph review,
  forget/supersede, episode summaries, consolidation) runs on daemon cadence
  or via the Cortex Console. Set `full` (or comment the line out) for admin
  sessions.

### Added (2026-07-10 — known-facts window for dream pass)
- Known-facts window for the dream pass (`memory.dream.known_facts_window`,
  default 0 = off): the extractor prompt shows current values of the top-N
  relevance-ranked slots so updates supersede in place instead of minting
  paraphrase keys. `--window` flags on `evals/longmemeval_bench.py` and
  `evals/ladder_sweep.py`; echo guard in `evals/window_echo_check.py`.
  (docs/specs/2026-07-10-known-facts-window-design.md)

### Added (2026-07-07 — Console: Extractor panel + dedup knobs)
- **Console Extractor panel** — the dream extractor endpoint is now
  switchable from the Cortex Console: base URL (with suggestions for the
  bundled sidecar, LM Studio, and Ollama), model name, call timeout, and max
  output tokens. A new `extractor_source` switch decides who owns these
  settings: `env` (default — the documented `PSEUDOLIFE_DREAM_*` ops
  contract, unchanged) or `config` (the panel's values win and the env vars
  are ignored; otherwise a UI change would silently lose to the env defaults
  the compose file always sets). All live — `build_extractor` constructs the
  client fresh on every dream invocation. The API key stays env-only in both
  modes (secrets never land in config.yaml); string knobs validate http(s)
  URLs at the write boundary.
- **New Console knobs** — `write_dedup_min_jaccard`,
  `alias_candidate_min_cosine` (Dream group) and
  `dream_slot_match_threshold` (Cortex group) are now editable live.

### Added (2026-07-07 — dream alias-candidate post-pass)
- **`MemoryService._propose_dream_alias_candidates`** — after a dream cycle
  writes its claims, every freshly-minted cortex entity name is cosine-
  compared (name embeddings) against existing entity names; the best match
  at/above `alias_candidate_min_cosine` (new `DreamConfig` knob, default 0.5,
  0 disables) files a merge proposal into the existing `entity_proposals`
  review queue — dismissed-pair suppression, unique-index dedupe, Atlas
  merge queue, and the accept/dismiss flows are all reused, and nothing is
  ever auto-folded. Semantic complement to the token-Jaccard write-dedup:
  paraphrase coreference ("production extractor sidecar" ~ "Pseudolife-MCP
  default extractor sidecar", Jaccard 0.33) embeds at cosine 0.65 while
  unrelated pairs calibrate ≤ 0.17 on all-MiniLM-L6-v2. Dream summaries gain
  an `alias_candidates` count.
- **`CortexStore.vocab_ranked` + `MemoryService._dream_vocab`**: the slot-key
  hint handed to the dream extractor is now ranked by cosine of each current
  slot's value-free embedding against the batch text, instead of taking the
  alphabetical head of the bank. On a bank larger than the ~60-key prompt
  window the alphabetical list rarely contained the keys a batch actually
  updates, so extractors minted paraphrase-variant entities instead of
  superseding (observed live 2026-07-06: a sidecar-version update never saw
  the existing `…sidecar.version` slot). Hint format is unchanged (the
  fine-tuned extractor was trained on it); alphabetical fallback on any
  ranking failure. KU-oracle re-run (e4b-ft, tag `vocabrank`): cortex 0.615
  vs 0.564, hybrid/rag within judge noise, ladder stale_leak 0.0 — the bench's
  per-question banks fit the window, so this mainly protects large live banks.
- **`evals/distill_clean.py`** — cleaning pass over the 2,000-row Qwen3.6-27B
  teacher-labeled extraction set (echo-key / spam-value / mega-row filters;
  1,756 rows / 7,823 claims kept).
- **`evals/distill_train_e4b.py`** — QLoRA SFT of Gemma-4 E4B on the cleaned
  set (WSL/4090, unsloth; pre-tokenized fixed 5120 shape so the graph compiles
  once, completion-only loss via manual −100 labels, eval batch 1, step-100
  checkpoints). `evals/distill_merge_e4b.py` re-merges from a checkpoint when
  the in-process merge is OOM-killed.
- **`evals/gate_e4b_ft.ps1`** + `e4b-ft` rung/extractor entries — acceptance
  gate for the fine-tune. Result: KU-oracle **cortex 0.564 / hybrid 0.769**
  vs base E4B QAT 0.359/0.551 — the 8B student beats its 27B teacher
  (0.397/0.590) on the task it was distilled for. Ladder: gold_recoverable
  1.0, stale_leak 0.0; CPU ~160s/question (same band as base E4B).
- **Deployed**: `ops/docker-compose.yml` mounts
  `evals/models/e4b-extractor-Q4_K_M.gguf` over the baked base model
  (drop the `volumes:` block to fall back). Verified live via a daemon dream
  cycle through the sidecar.

### Changed (2026-07-06 — default extractor sidecar E2B → E4B QAT)
- **`ops/Dockerfile.extractor` now bakes Gemma-4-E4B QAT (UD-Q4_K_XL, ~4.2GB)**
  instead of E2B QAT. The LongMemEval knowledge-update bench showed E4B builds
  a far stronger fact spine (cortex 0.333-0.359 vs E2B's 0.192; hybrid
  0.551-0.564 vs 0.474) at only ~1.4x E2B's CPU wall time per dream cycle.
  Qwen3.5-4B (higher still on GPU) was disqualified as a CPU sidecar: its
  verbose extractions deterministically overrun the generation cap on large
  batches (5.7x wall time with multi-minute retry tails). Constrained machines
  can bake E2B back via the documented `MODEL_URL` build-arg.
- **`--parallel 1` pinned in the extractor CMD**: newer llama.cpp server images
  default to 4 slots sharing one unified KV buffer, so two concurrent ~4k-token
  dream calls exceed the context and every request fails with "Context size has
  been exceeded". One slot restores the serialized behaviour older images had.
- **`PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` default 240 → 480** — E4B generates at
  roughly half E2B's CPU token rate, so a full 2048-token extraction needs the
  extra headroom.

### Changed (2026-07-06 — cortex retrieval floor lowered)
- **`memory.cortex.guard_min_score` default 0.3 → 0.2.** A LongMemEval
  retrieval replay (`evals/retrieval_sweep.py` over dumped fact banks) showed
  the 0.3 floor served *zero* cortex facts for 60% of questions: fact
  embeddings are terse `entity attribute value` strings whose cosine against
  a natural-language query rarely clears 0.3 even when the fact is the
  answer. 0.2 halves starvation (60% → 28%) at identical end-to-end accuracy
  in the before/after judge run (`evals/rebuild_contexts.py`). 0.1 was tried
  and rejected: it un-starves further but the extra weak facts dilute the
  context and the answerer abstains on questions it previously got right.
  Abstention-on deployments should keep overriding upward (`0.65` pairing,
  see README). The bench's cortex arm moved to `top_k=24, min_score=0.2`.
- **Phantom entry ids from a connection lost mid-store**: psycopg's
  transaction block exits *silently without committing* when the connection
  broke during the block (`pgconn.status != OK`), so `insert_entry` could
  return a RETURNING id for a row the server rolled back. `_txn` now verifies
  the transaction actually committed and raises `OperationalError` otherwise,
  so every mutator reports the loss instead of pretending success.
- **Permanent dream stall on `memory_traces_entry_id_fkey`**: when in-memory
  entries held db_ids absent from the entries table (the rolled-back-insert
  case above), every `dream_run` trace write hit the FK violation and the
  claim-write hold retried the SAME write each sweep — a stall only a process
  restart cleared (seen 2026-07-04 in evals bench runs). On a claim-write
  failure the dream now verifies the pulled batch's in-memory→PG entry
  mapping and re-flushes entries whose rows are gone (fresh row + id), so the
  hold resolves on the next sweep. New: `PostgresStorage.existing_entry_ids`,
  `CMS.reflush_entries`, regression tests in
  `tests/test_connection_loss_recovery.py`.

### Added (2026-07-04 — final polish batch)
- **Keyboard operability for click-only rows**: cortex fact rows, episode
  timeline items, Insight god-node rows and community rows, and Atlas
  provenance chips are now focusable and Enter/Space-activatable (new
  `pressable()` helper in `util.js`), each with an `aria-label`. The Insight
  god-node row is no longer a `<button>` wrapping another interactive element
  (invalid HTML) — the atlas jump is a real button inline with the name.
- **Confirm gate on "Dismiss" for duplicate findings** — marking a pair as
  genuinely distinct permanently stops it resurfacing, so it now confirms
  like the other irreversible actions (ordinary proposal rejects stay
  one-click for fast triage).
- **Graph → Cortex deep link**: the node panel's "Facts ↗" opens the Cortex
  view pre-filtered to that entity (`#/cortex?q=…`) instead of unfiltered.
- **`examples/CLAUDE.memory.md`** — the recommended CLAUDE.md memory block as
  a copyable file; **`docs/README.md`** — marks docs/ as internal design
  history and maps its subdirectories.
- **Manifest doc gaps**: `memory_search` enumerates the eight band names;
  `memory_forget` contrasts its OR-combined filters against search's AND.
### Added (2026-07-04 — UX fast-follow, P2 batch)
- **Recall tab explains itself**: a first-visit intro describes multi-hop vs
  path-between-two with runnable example queries; seed chips cap at 15 behind
  a "+N more" expander (with a one-line "what is a seed" hint); entities
  without canonical facts collapse into one compact chip block instead of a
  panel each.
- **`ops/install-hook.sh`** — Linux/macOS port of the briefing/episode hook
  installer (python3-based JSON edit, backup-first, idempotent, preserves
  existing hooks). Both installers stop writing the unrecognized
  `"shell": "bash"` field.
- **README Troubleshooting + Uninstall sections** — the scattered fixes
  (WSL `Vmmem` cap, port-forward loss after `wsl --shutdown`, first-build
  expectations, 401/offline meanings, `claude mcp list` check) collected
  under one heading; uninstall documents the deliberate volume-removal path.
- **Core tier grows by two**: `memory_get` (core `memory_fact_get` returns
  `source_entries` ids that core mode couldn't dereference) and
  `memory_session_title` (the recommended workflow names the session early).

### Fixed (2026-07-04 — UX fast-follow, P2 batch)
- **`memory_search` always returns the `cortex` key** (empty list on a miss —
  previously the documented key was absent, so `result["cortex"]` could
  KeyError).
- **Console a11y + interaction**: modals are `role="dialog"` with a focus
  trap and focus-restore-to-opener; toasts announce via `role="status"`;
  `confirmDialog` resolves `false` on backdrop/ESC close (previously the
  awaiting caller hung forever); keyboard shortcuts ignore Ctrl/Cmd/Alt
  chords and `0` reaches the tenth tab; the config editor treats an emptied
  number field as "no edit" instead of sending `""` (raw `float('')` error);
  the Observatory band-count subtitle reads from the live preset instead of
  a hardcoded "8".

## [0.7.0] - 2026-07-04

(Header restored 2026-07-16: the original 0.7.0 cut (3ab06fc) placed it here;
a same-day edit (60bdf61) accidentally replaced the header line with its own
subsection, silently dissolving the release boundary for twelve days.)

### Fixed (2026-07-04 — pre-release UI/UX pass)
- **`memory_outcome` no longer coerces an unknown outcome to `"success"`**
  (which could invert a typo'd failure signal into a do-this lesson). An
  invalid value is refused up front — `{recorded: false, reason:
  "unknown_outcome", outcomes: [...]}` — and the tool schema now rejects it
  client-side too.
- **Console: one 401 notice instead of a storm.** With a token-gated daemon,
  the parallel boot fetches produced a stacked "Unauthorized" toast + token
  modal rebuild per call; the notifier is now latched until the token changes
  or a call succeeds. The token input is a `password` field.
- **Console: honest topbar.** An unreachable daemon shows an `offline` chip
  instead of the green pulsing `live`.
- **Console: no silent truncation.** `/api/facts`, `/api/world`, and
  `/api/lessons` return `total` + `truncated` alongside the capped `entries`,
  and the Cortex/World/Lessons views render "first N of M loaded" when the
  bank exceeds the fetch limit (the live bank's 1,358 facts were silently
  capped at 1,000 with no indication).
- **Console: Atlas review panel now follows both themes** — it referenced
  undefined `--surface-*` design tokens, so its chips/borders always fell
  back to hard-coded greys.

### Changed (2026-07-04 — MCP tool-surface ergonomics)
- **Verb-dispatch and enum params are typed `Literal`** so the JSON schema
  itself enumerates the legal values (`memory_dream.action`,
  `memory_forget.scope`, `memory_graph_review.action`,
  `memory_outcome.outcome`, `memory_world_set.freshness_class`,
  `memory_store.origin`, `memory_fact_set.origin`) — dispatch is discoverable
  from the manifest alone; the in-body structured-error fallbacks remain for
  direct callers.
- **Uniform failure contract**: a tool body that raises now returns the same
  `{"error", "message"}` shape the dispatch tools use, instead of leaking a
  raw exception string (e.g. `document_ingest` on a missing file).
- **`document_ingest` documents server-side path resolution** — with the
  Docker daemon the path must be visible inside the container.

### Added (2026-07-04 — docs & release hygiene)
- **README Quickstart** (clone → volumes → compose up → `claude mcp add` →
  verify) with the wiring step that was missing entirely: where the
  `mcpServers` JSON lives (`~/.claude.json` / project `.mcp.json`) and the
  `claude mcp add --transport http` one-liner.
- **Mechanical doc-drift guards** (`tests/test_release_ux.py`): README schema
  version must match `SCHEMA_META_VERSION` (it had drifted three separate
  times), no hardcoded test-count claims, and the Claude-Code wiring
  instructions must exist. Schema/capabilities rows corrected to v21; the
  stale 60-line Testing narrative replaced with a count-free summary; the
  broken `%USERPROFILE%` shim example fixed to `${USERPROFILE}`.

### Added (2026-07-04 — LongMemEval knowledge-update results)
- **First external-benchmark results** (`evals/results/longmemeval-ku-*`):
  LongMemEval knowledge-update subset (78 questions), floor (Gemma-4-E2B) +
  ceiling (Qwen3.6-27B) extractors, answerer/judge pinned to local Qwen3.6-27B.
  Oracle: hybrid 0.705 vs naive-RAG 0.615 at ~40% less context; cortex alone
  0.564 at 59 ctx tok/q (3.6% of RAG's budget). Full `_s` haystacks
  (~48 sessions/q): hybrid 0.372 vs RAG 0.321. The RAG control stays flat
  across extractors while cortex drops 0.564 → 0.192, isolating extraction
  quality as the fact-spine bottleneck. Overnight runner hardening: full
  `.bat` path (`NoDefaultCurrentDirectoryInExePath`), `Write-Host` logging
  (return-value pollution), and the bench now aborts only on a dead extractor
  endpoint (probe-gated, 8-hold cap) instead of on any transient extraction
  failure.

### Added (2026-07-03 — bigger-local-model extractor docs + compose overrides)
- **`PSEUDOLIFE_DREAM_*` are now overridable via `ops/.env`** (compose
  interpolation with the sidecar as the default), so the Dockerized daemon can
  point dream consolidation at LM Studio / Ollama / llama.cpp / vLLM without
  editing the compose file; commented `extra_hosts` snippet for Linux
  `host.docker.internal`. README gains an "Upgrading the extractor" section
  with per-runtime base URLs and the ladder-measured upgrade guidance.

### Fixed (2026-07-03 — dream extraction supersession regression)
- **Dream extraction is batched again**: the 2026-06-25 per-entry restructure
  (added for per-claim source traces) meant the extractor never saw a fact's
  initial and update turns together, so it named them inconsistently and
  updates landed on sibling slots instead of superseding — ladder stale-leak
  went 0.0 → 0.7–0.9 (all quants equally; the 06-24 QAT model swap was
  unrelated). `dream_run` now sends the whole pull in ONE numbered-notes call
  and the model cites each claim's source note (`"source"`), keeping trace
  attribution. Poison-entry quarantine survives via a per-entry isolation
  fallback after repeated batch failures, with an all-fail outage guard that
  holds the cursor instead of quarantining.

### Added (2026-07-03 — community files)
- **CONTRIBUTING.md** (dev setup, offline test invocation, live-bank safety
  rules, DCO sign-off, permissive-only dependency policy) and **SECURITY.md**
  (private vulnerability reporting, threat model, in/out of scope).

### Added (2026-07-03 — cross-platform ops)
- **Bash ops scripts**: `ops/backup.sh`, `ops/restore.sh`, `ops/update.sh` —
  feature-parity ports of the PowerShell originals for Linux/macOS hosts
  (same rehearse-by-default restore, rollback-tagged daemon-only update, and
  off-disk backup mirror). `.gitattributes` pins `*.sh` to LF.

### Changed (2026-07-03 — cross-platform ops)
- **Postgres password is overridable**: set `POSTGRES_PASSWORD` in `ops/.env`
  before first launch (compose default remains `pseudolife`, guarded by the
  loopback-only port binding; the daemon's `DATABASE_URL` follows the same
  variable).
- **Daemon image tag aligned to the package version** (`pseudolife-daemon:0.6.0`,
  was `0.2.0`), and both update scripts now read the tag from the compose file
  instead of hardcoding it — one source of truth for future bumps. Existing
  installs: the next `update.ps1`/`update.sh` run simply builds the new tag;
  old `0.2.0`-tagged images can be `docker image rm`'d at leisure.

### Changed (2026-07-03 — public-release licensing prep)
- **License: MIT → Apache-2.0** (LICENSE replaced with the canonical text,
  NOTICE added, pyproject + README updated). Apache-2.0 keeps the same
  permissive terms and adds an explicit patent grant.
- **Optional PDF extra: PyMuPDF → pypdfium2** (`pip install .[pdf]`).
  PyMuPDF is AGPL-3.0, which conflicts with permissive distribution and any
  future commercial/hosted offering; pypdfium2 (Chromium PDFium bindings,
  Apache-2.0/BSD-3) fills the same higher-quality-extraction slot. The core
  pypdf fallback is unchanged.

### Added (2026-07-03 — dream near-duplicate correction, schema v21)
- **Write-time dedup (Tier 1)**: a dream-minted entity whose name-token
  Jaccard against an existing canonical/display/alias reaches
  `memory.dream.write_dedup_min_jaccard` (default 0.6; 0 disables) files an
  `entity_proposals` merge row at birth — dismissed pairs suppressed, advisory
  only, never blocks the write. Explicit relate/fact writes untouched.
- **Deep-dream merge triage (Tier 2)**: `memory_dream(action="deep")`
  responses carry `merge_proposals` — pending near-duplicate merges enriched
  per side with display/etype/degree/scopes/snippets (`into` = higher-degree
  side); the `/dream deep` driver instructs the capable model to
  `accept_merge` same-referent variants, reject + `dismiss_pair` distinct
  ones, and leave unsure items for Atlas.
- **Merge-decision audit**: new FK-free `merge_decisions` table (an accepted
  merge CASCADE-deletes its proposal row, so the audit is denormalized) +
  `decided_by`/`decided_at` stamps on entity proposals; MCP decisions stamp
  `agent`, Console `human`; `/api/graph/review` and Atlas show
  "recent merge decisions" newest-first.

### Added (2026-07-03 — external findings wave 2)
- **Lesson staleness ("re-verify")**: lessons whose `about` entity saw cortex
  fact churn after the lesson was asserted/confirmed carry `re_verify` +
  `re_verify_reason` in `memory_lesson_search` / `/api/lessons`, and the
  session briefing renders a `⚠ re-verify` suffix. Read-time only — no stored
  state; re-confirming the lesson clears the flag.
- **Causal chain ("what led to X")**: `memory_history(entity)` without an
  `attribute` now returns the entity's dated event chain — canonical fact
  assertions/supersessions, source entries (with episode titles), graph
  edges, and lessons, merged oldest→newest. Also `GET /api/chain` and a
  timeline block in the Console's entity-provenance drawer.

### Added (2026-07-03 — external findings wave 1)
- **Betweenness god-nodes**: `god_nodes()` now ranks by betweenness centrality
  (bridges whose loss disconnects communities) with degree as tiebreak; each
  item carries a new `betweenness` field alongside `degree`. K-sampled above
  `memory.graph_insight.betweenness_sample` nodes.
- **`memory_fact_get` candidates on miss**: an empty slot (no record, no
  contenders) now returns `candidates` — same-entity slots first
  (recency-ranked), then embedding-similar slots above a 0.35 floor — ranked
  leads instead of a bare null.
- **Edge provenance tags**: graph edges surface a derived
  `EXTRACTED | INFERRED | AMBIGUOUS` tag (origin user/action; agent at working
  confidence; proposals + sub-0.5 confidence) in `memory_graph`, `/api/graph`,
  review findings, and as Atlas/Console badges. No schema change.

### Added (2026-07-03 — deep-dream review follow-ups)
- **Pre-apply graph snapshot**: `memory_dream(action="deep", apply=true)` first
  dumps the five graph tables to
  `data_dir/graph_snapshots/graph-<stamp>.json` (newest
  `memory.deep_dream.snapshot_keep` kept, default 10) and refuses with
  `snapshot_failed` — writing nothing — if the dump fails. Response carries the
  `snapshot` filename.
- **`memory_graph_review(action="dismiss_pair", src, dst)`**: record a
  "genuinely distinct" verdict over MCP (wires `graph_dismiss_duplicate`), so
  the Step-C agent can stop noise pairs resurfacing as deep-dream candidates.
- **Step-C driver flow**: `/dream deep` in `examples/commands/dream.md` +
  updated `docs/runbooks/deep-dream.md` — judge candidates from snippets, then
  propose / dismiss_pair / leave for Atlas.

### Changed (2026-07-03 — deep-dream review follow-ups)
- **Candidate snippets truncated** to `memory.deep_dream.snippet_max_chars`
  (default 240) — the full-length deep response had outgrown MCP output limits
  (~483KB) — and `memory_dream(..., snippets=false)` omits them entirely.
- **Support-overlap filter**: `candidate_pairs` drops pairs whose
  supporting-entry sets overlap at Jaccard >=
  `memory.deep_dream.max_support_overlap` (default 0.8), generalizing the old
  identical-set (co-occurrence) drop and killing the same-doc verb-cluster
  noise.
- **Dry-run/apply parity**: `would_merge_propose` / `would_junk` items are
  annotated `already_proposed` when an entity_proposals row (any status)
  already covers them — the preview now predicts the apply counters.

### Added (2026-07-02 — episode naming + fragmentation rework)
- **Episode consolidation primitives**: `service.episode_rename(id, title)`
  and `service.episode_merge(sources, into?/title?, hint?)` re-stamp the
  denormalised `episode_id`/`episode_title` on band entries (in-memory + DB),
  bulk-retarget evicted entries and `outcome_signals`
  (`PostgresStorage.retarget_episode_refs`), re-parent child episodes, widen
  the target span, and delete the merged husks. REST:
  `POST /api/episodes/rename`, `POST /api/episodes/merge`. Open sources are
  skipped (`skipped_open`) — a live session is never merged away.
- **Resume-on-return**: a store arriving after the idle reaper closed the
  session's episode now *reopens* that episode (same `mcp-session-id` = same
  client session) instead of opening a fresh generic husk.
  `PSEUDOLIFE_SESSION_RESUME_SECONDS` (default 21600 = 6 h) bounds the window;
  `0` disables.
- **Auto-title at close**: a session episode still carrying the generic
  `session - YYYY-MM-DD HH:MM` lazy-open title gets a derived
  `"{dominant_source} - {stamp}: {first-entry snippet}"` title when it closes
  (explicit end or reaper). Agent-set titles never match the generic pattern
  and are untouched (`session_title.derive_session_title`).
- **Untitled-session nudge**: `memory_store` responses include a one-line
  `episode_hint` while the session episode is still generic-titled, pointing
  at `memory_session_title`.

### Fixed (2026-07-02 — episode naming + fragmentation rework)
- `memory_episode_start` called before the session's first store now lazily
  opens the session root first and nests under it, instead of creating a
  session-keyed root that `memory_session_title` would then rename.
- `memory_session_title` now also rewrites the denormalised `episode_title`
  stamp on entries already stored in the session.

### Changed (2026-07-02 review, final item — MCP tool-surface consolidation)
- **BREAKING: the MCP surface shrank from 55 tools to 32** (the manifest is
  agent context every session: description payload dropped ~37.0k → ~15.0k
  chars, ~60%). Three verb-dispatched tools replace fifteen:
  - `memory_dream(action=...)` — `status` / `pull` / `commit` / `run` /
    `deep` (replaces `memory_dream_status/pull/commit/run` +
    `memory_deep_dream`).
  - `memory_forget(scope=...)` — `memory` / `fact` / `world` / `lesson`
    (replaces `memory_delete`, `memory_fact_forget`, `memory_world_forget`,
    `memory_lesson_forget`). Scope `memory` now returns a structured
    `{error: "filter_required"}` on a filterless call instead of a raw
    ToolError.
  - `memory_graph_review(action=...)` — `list` / `propose` / `accept_link` /
    `reject_link` / `accept_merge` / `accept_junk` / `reject_entity`
    (replaces `memory_graph_propose_links` + the five accept/reject tools;
    `list` newly exposes `service.graph_review` over MCP).
- **Removed from MCP** (Console REST + CLI cover them; service methods and
  `/api` routes unchanged): `memory_facts`, `memory_world_facts`,
  `memory_lessons`, `memory_list_sources`, `memory_list_tags`,
  `memory_episode_list`, `memory_communities`, `memory_digest`,
  `memory_briefing` (the SessionStart hook uses `pseudolife-mcp briefing`),
  `memory_path` (use `memory_graph(to=...)`), `memory_save` (autosave loop +
  exit flush already cover durability).
- **Every remaining docstring rewritten terse** — first line says what the
  tool does, when-to-use guidance kept only where it changes behaviour.
  `tests/test_tool_consolidation.py` pins the budget (≤1600 chars/tool,
  ≤18k total) plus dispatch/validation contracts for the three merged tools.
- Core-tier membership (`PSEUDOLIFE_MCP_TOOLSET=core`) is unchanged — all 15
  core tools kept their names, as did every tool referenced by the global
  CLAUDE.md workflow. `/dream` command and deep-dream runbook updated to the
  new verbs.

### Fixed/Changed (2026-07-02 review P3 — surface polish + zombie sweep)
- **Tokenless `/api` is now browser-hardened** (review H2, live exposure —
  the daemon runs without a token): foreign `Origin` → 403 (CSRF, covers
  bodyless POSTs), foreign `Host` → 403 (DNS rebinding), and any POST with
  a body must be `application/json` → 415 (a cross-site form can't send
  that without a failing CORS preflight). Non-browser clients send neither
  header and pass; with a token set the host gates are skipped (the
  Authorization header already proves intent, so LAN use stays legitimate).
- **Console Stream view repaired**: search/recent entries now carry the
  storage row `id` (the engram-trace button finally renders live, and
  agents can pair hits with `memory_get`/`memory_reinforce`); the "Explain
  ranking" drawer reads the real trace keys (`name`/candidate lists/
  `text_preview`) instead of fixture-invented ones that rendered
  "undefined" and "[object Object]" in production.
- **Fixture-vs-serializer contract test** (`tests/test_fixture_contract.py`)
  pins the exact keys the Stream view consumes against both the real trace
  and the devserver fixtures — fixture drift now fails CI instead of QA.
- **ReferenceBank similarity math**: ChromaDB cosine distance is `1 − cos`,
  so similarity is `1 − dist` — the old `1 − dist/2` scored orthogonal
  chunks 0.5, above the retrieval floor, appending unrelated documents to
  essentially every search once any document existed.
- **BM25 tokenizer keeps standalone integers** (`port 8080`, `error 404`,
  `RTX 4090`) — the numeric pattern required a dot, silently gutting the
  exact-token channel for the very tokens it exists to catch.
- **Zombie sweep**: removed the never-called `ContrastiveUpdater` /
  `ContextBuilder` daemon wiring, the dead `AuthHealthASGI` wrapper, the
  chat-product config blocks (`backend`/`claude`/`gemini`/`lmstudio`) and
  `HydeConfig`; `NLIConfig.enabled` now defaults False with an honest
  "not wired" docstring; the HNSW index on `entries.embedding` is dropped
  (maintained on every insert, queried by nothing — similarity runs in
  Python over the hydrated bands).
- **Session titles no longer mis-attribute on POSIX** (found by CI run #1):
  a Windows-style client cwd parsed as one relative segment on Linux, so
  the git walk could title a session after the daemon's own repo.

### Added (2026-07-02 review P2 — quality infrastructure)
- **CI (GitHub Actions).** `.github/workflows/ci.yml` runs the full suite on
  every master push and PR: pgvector/pg16 service container, CPU-torch
  install mirroring the daemon image, cached pip + HuggingFace models, then
  the documented offline invocation. Budget-guarded for a free-plan private
  repo: master+PR triggers only, cancel-in-progress concurrency, no
  artifacts (warm run ≈ 4-6 min of the 2,000 free minutes/month).
- **Retrieval golden set** (`tests/test_retrieval_golden.py`): 50 realistic
  memory/paraphrase-query pairs asserting recall@5 ≥ 0.92 and MRR ≥ 0.85 on
  the dense path plus top-3 ≥ 0.85 on BM25-fused identifier queries
  (measured baseline: 1.000 / 0.990 / 1.000) — the first thing on master
  that can catch a *ranking* regression, in under a second.
- **`ops/restore.ps1`** — the restore path is now a rehearsed procedure, not
  a code comment. Default mode restores the newest backup into a scratch
  database, reports per-table row counts against the live bank, and drops
  the scratch (live bank untouched); `-Apply` does the real restore with a
  pre-restore safety dump, daemon stop/start, and a health gate. Rehearsed
  2026-07-02 against the latest backup: PASSED.
- **Off-disk backup mirror**: `ops/backup.ps1` copies each artifact to
  `PSEUDOLIFE_BACKUP_MIRROR` (or `-MirrorDir`) with the same retention when
  set — point it at a folder on another physical disk. Mirror failure warns
  but never aborts (the primary backup already succeeded).

### Changed (2026-07-02 review H4 — autocommit connection)
- **Reads no longer leave the shared connection idle-in-transaction.** The
  storage connection runs autocommit; every mutation opens an explicit
  psycopg transaction block (`_txn` → `conn.transaction()`, nesting degrades
  to savepoints). Pre-fix, a bare read opened an implicit transaction that
  stayed open until the next mutator committed — pinning the xmin horizon
  overnight (blocking autovacuum) and holding ACCESS SHARE locks that
  blocked any concurrent DDL (the root cause of the test-suite
  lock-timeout ordering flake). `ensure_schema` now wraps its DDL in one
  `conn.transaction()` block so it stays atomic under both connection modes.

### Changed (2026-07-02 review P1 — per-slot persistence, schema v19)
- **The full-table snapshot rewrite is gone from the write path.** Every
  cortex/world/lesson write used to `DELETE FROM <table>` and reinsert every
  row (embeddings included) — O(claims × total rows) per dream sweep,
  permanent id churn and autovacuum pressure, and a structural blocker for
  the dormant OCC seam. The stores now track `dirty_slots`; saves persist
  only the mutated `(entity_norm, attribute_norm)` slots in one transaction
  (`replace_slot_facts` / `_world_facts` / `_lessons`,
  `sync_cortex_slots` / `sync_world_slots` / `sync_lesson_slots`). The
  supersession log + dream cursor ride a `meta_dirty` flag instead of being
  rewritten every save. Full snapshots remain for explicit `memory_save`,
  exit flush, and restore/migration — a belt-and-braces resync.
- **Schema v19:** partial unique indexes enforce one `current` row per slot
  on facts/world_facts/lessons (+ at-most-one `contested` on facts) — the
  invariant previously lived only in Python, so an additive `restore_from_pt`
  could silently create duplicate current rows. `ensure_schema` heals
  pre-existing duplicates first (keeps the most recently confirmed, demotes
  the rest — mirroring `CortexStore._reindex_current`).
- **HLC re-seeds from stored stamps at hydrate** (`hlc.observe` of the
  bank's high-water mark): a wall-clock step-back across restarts (NTP,
  resume) no longer lets history outrank new writes and park user
  corrections as contenders.
- **Auto-promoted facts are stamped** (`_promote_slots` now passes HLC +
  writer/session like `cortex_write`): unstamped rows could never supersede
  stamped ones and were retro-labeled `writer_id='legacy'` by the v11
  backfill on every boot.

### Fixed (2026-07-02 review P0 — six correctness fires)
- **MCP tools no longer block the daemon's event loop.** The SDK invokes sync
  tools inline on the uvicorn loop, so one long call (`memory_dream_run`,
  `document_ingest`, first-call model init) froze every other session,
  `/health`, and the Console — a Docker healthcheck could kill the daemon
  mid-dream. Every registered tool is now an async wrapper that dispatches
  its sync body via `anyio.to_thread.run_sync` (one change in `_tool()`;
  module-level fns stay sync for the Console/tests). Contextvars (writer /
  session attribution) propagate into the worker thread.
- **Postgres reconnect + honest `/health`.** A PG restart used to poison the
  daemon permanently (single connection, no reconnect anywhere) while
  `/health` — which never touched the DB — kept saying "ok". `storage.conn`
  is now a heal-on-next-use property; `/health` pings the DB on a dedicated
  short-lived connection and reports 503 `status:degraded` when it's
  unreachable. `_txn` rollback on a dead connection no longer masks the
  original exception. `ensure_schema`'s DDL timeouts are now `SET LOCAL` —
  the old session-wide `SET` silently capped every runtime query at 30s.
- **`access_count` now counts returned results, not candidates.** Bands
  bumped every band-local top-k candidate (up to 8 per band per query,
  pre-filter), corrupting promotion, MTT retention, and eviction scoring at
  the source. The bump moved to the final merged top-k in `cms.retrieve`.
- **Eviction prefers superseded entries.** A correction arrives with
  near-zero surprise while the stale fact it replaced keeps a decayed-but-
  larger one, so surprise-driven eviction destroyed corrections and kept the
  stale facts. Superseded entries now score 0.05× — always the cheaper loss.
- **Graph ingestion gated at the source** (the junk root cause, previously
  patched detection-side only): dream relations drop endpoints matching the
  known junk classes (`junk_name_reason`: concat-artifacts, bare numbers,
  status words) before entity creation; fact-write subject nodes get the same
  gate; `dream.min_relation_confidence` default 0.0 → **0.2** (hard
  type-violations score ≤0.175 and are now dropped, not written-then-cleaned);
  and `upsert_edge(revive=False)` on the dream path makes human removals
  sticky — an agent re-assertion no longer resurrects a superseded edge.
- **Dream poison-pill quarantine + idempotent re-dreams.** A deterministically
  failing entry used to stall consolidation forever (same batch retried every
  sweep) while each retry re-confirmed the batch prefix, ratcheting agent-
  guess confidence toward ~0.98. Now: three strikes per entry → quarantine
  (cursor advances past it), and an already-traced (slot, source-entry) pair
  is skipped on re-extraction instead of re-confirmed.
- **User config.yaml keys are respected.** `_apply_mcp_defaults` clobbered
  five knobs unconditionally after load (`surprise_threshold`,
  `meta_filter.enabled`, `recency_base_half_life_s`, `traces.retention_boost`,
  `embedding.batch_size`) — the YAML knobs were dead. Defaults now overlay
  only keys the user left unset; `load_config` also gained the missing
  `memory.traces` / `memory.deep_dream` sections.
- **Lesson signals survive empty synthesis.** `synthesize_lessons` consumed
  the outcome-signal queue even when the extraction wrote nothing — silently
  losing the only feeder for procedural memory. Signals are now consumed only
  when at least one lesson landed.

### Added
- **Session-scoped episodes (correct attribution + clean names).** Episodes are now
  keyed to a **stable per-session id** instead of a single global `current_id`, so a
  new session (e.g. a different project) no longer auto-closes another's open episode
  and each `memory_store` is stamped to *its own* session's episode even under
  concurrency (`EpisodeManager` tracks one open episode per `session_key`). The session
  id is the transport's `mcp-session-id` — **stable per session for a direct-HTTP
  client** (the daemon's shipped transport), or a stdio shim's `X-PL-Session`
  (`writer_context` prefers it). **Lifecycle is daemon-owned:** because a direct-HTTP
  client has no shim/hook in the path, the daemon **lazily opens** a session episode on
  the first store of a new session id (so empty sessions never create a husk) and an
  **idle reaper** closes it once inactive — firing the end-of-session dream, or pruning
  it if empty (`PSEUDOLIFE_SESSION_IDLE_SECONDS`, default 30 min). The
  `SessionStart`/`SessionEnd` `episode-start`/`episode-end` hooks are therefore obsolete
  (removed; the legacy CLI + shim path remain for stdio clients). Titles are
  `{project} - {YYYY-MM-DD HH:MM}` (shim, from cwd) or `session - {YYYY-MM-DD HH:MM}`
  (daemon, generic — direct-HTTP carries no project signal; set `TZ` in `ops/.env` for
  local-time titles). New `storage.delete_episode` +
  `service.episode_prune_empty(include_open=False)` + `POST /api/episodes/prune` provide
  a one-shot cleanup for the empty/spurious husks the old single-pointer model
  accumulated. New `memory_session_title(title)` tool lets an agent name its
  session episode (since the daemon can't see the client's project dir); the
  shim no longer titles GUI-client sessions after a system dir (`system32` →
  generic `session`).
- **Atlas review queue: granular per-item bulk actions.** The `dubious_edge` (Prune),
  `unattributed` (Assign) and `test_artifact` (Delete) findings — previously
  all-or-nothing over the whole list — now render a filterable, capped-scroll checkbox
  list with "select all (filtered)" / "clear" and a live count on the action button
  (opt-in: nothing selected by default), so you act on exactly the chosen subset. The
  `orphan` finding is now actionable too (Delete + Assign on the selection). Pure
  frontend (`atlas_review.js` `selectableList`) — the findings already carried their
  full lists and the handlers already post per item.
- **Atlas review queue: entity provenance.** New `GET /api/graph/entity-provenance`
  (`service.entity_provenance` + `storage.entries_for_entity`) returns an entity's
  project attribution (`entity_sources`: source · count · origin) and the MIRAS
  source entries behind its facts (band · source · ts · text), bridging
  `facts.entity_id → entity_norm → memory_traces → entries`. In the Atlas Review
  panel every entity name is now clickable to lazy-load a provenance drawer, so a
  human can judge a merge/junk/link finding from real evidence, not names alone.
  (Source entries carry no user/action/agent tier — that lives on facts/edges — so
  the drawer shows band + `entity_sources` origin, not a per-entry tier.)
- **Session-start briefing (P1.7).** New `memory_briefing` tool + `pseudolife-mcp
  briefing` CLI assemble a "what your memory is unsure about" (graph surprises +
  questions) + "lessons from past work" (avoid/prefer) block. Wire the CLI to a
  SessionStart hook (README) to auto-inject it; it never auto-starts the daemon
  and prints nothing on a cold bank.
- **Easy hook install + safe updates.** `pseudolife-mcp briefing --hook-json`
  emits the SessionStart `additionalContext` payload; `ops/install-hook.ps1` wires
  it idempotently alongside existing hooks (backs up `settings.json` first);
  `ops/update.ps1` does a backup-first, daemon-only (`--no-deps`) rebuild that
  never touches Postgres/the extractor or runs `down -v`.

### Changed
- **Dream cadence: faster post-activity consolidation.** `memory.dream.idle_seconds`
  default 1800 → 600, so the cortex consolidates ~10 min after you go quiet (still
  never mid-session — any store resets idle). The quiescence gate logic is
  unchanged. The README "Dreaming" section now documents the concrete cadence
  (8 / 600s / 600s, daemon-only) and the on-demand `memory_fact_set` /
  `memory_dream_run` paths.
- **Tool-surface gate + redundancy trim.** `PSEUDOLIFE_MCP_TOOLSET=core` exposes a
  lean 15-tool core set (default `full` = unchanged). Folded `memory_trace` into
  `memory_search(explain=True)` and dropped `get_neighbors` (its `relation_filter`
  moved onto `memory_graph`); `memory_path` retained. 48 → 46 tools at the time
  of this change (the surface has since grown again with the deep-dream /
  entity-consolidation additions below — see README for the current count).
- **Retention bench made honest (P1.6).** `evals/retention_bench.py` now models a
  heavy-tailed reinforcement workload with `access_count` coupled to reinforcement
  (reinforcing *is* accessing). The honest re-derivation keeps `retention_boost=1.0`
  (the largest boost with ~no recency displacement) but shows it's a modest nudge on
  top of the automatic access-coupling — not the dramatic knee the prior synthetic
  bench implied. Default unchanged.
- **Right-sized the continuum bands.** The default `continuum` preset's total
  capacity drops 44,000 → ~5,250 (e.g. `slow` 8000→1500), all still well above a
  personal bank's fill — so eviction/curation engages in ~1 year (the `slow`
  band) instead of ~decades, with no data loss on existing personal banks. Raise
  the caps (or use `preset: custom`) for high-volume / multi-agent deployments.

### Fixed
- **Atlas review queue rendered deep-dream findings unusably.** The panel predated
  the deep-dream proposal shapes: `merge_candidate` (data in `f.merges`),
  `proposed_link` (`f.links`), and `junk_candidate` (`f.entities` as objects) showed
  no detail or literal `[object Object]`, and their action buttons were dead (read
  `f.entities` → `[undefined, undefined]`) or posted malformed bodies. The renderer
  (`atlas_review.js`) now understands all finding shapes and surfaces the
  already-computed signals (jaccard / similarity / confidence / reason / rationale);
  the handler (`views/atlas.js`) dispatches per item to the id-keyed
  `accept-entity-merge` / `accept-entity-junk` / `reject-entity-proposal` /
  `accept-proposal` / `reject-proposal` endpoints. `graph_review.proposed_links` now
  carries the `edge_proposals` id so links are accept/reject-able.
- **"Merge duplicate entities" modal clipped long names.** The footer put the full
  entity name in each button (`Keep "<name>"`); long path-like names overflowed the
  fixed-width modal (`overflow:hidden`) and were cut off. The modal now shows both
  full names (labelled A/B, wrapping) in the body and uses short, middle-ellipsised
  button labels; `.modal-foot` also wraps (`flex-wrap`) as a safety net.
- **Deep-dream merge proposals were noisy; `A<->B` artifacts were unhandled.** The
  entity-merge classifier proposed a merge whenever one name's token set was a subset
  of the other's, so single generic tokens drove false merges (`memory_graph→Graph`,
  `bank→live bank`, `LIVE→live daemon`) and real entities were merged *into*
  concatenated extraction artifacts (`Phase 2 plan → Phase 1 plan<->Phase 2 plan`).
  `_name_contains` now requires the contained token set to have ≥2 tokens and excludes
  any concat-artifact endpoint; a new degree-agnostic `concat-artifact` junk rule
  (`_is_concat_artifact`) surfaces the `A<->B` nodes for deletion instead.
- **A failed statement could wedge the whole daemon (connection poisoning).** The
  daemon holds one long-lived psycopg connection, but only 3 of ~30 mutating methods
  in `storage/postgres.py` rolled back on error — so any raised statement (lock
  timeout, FK violation) left the connection `InFailedSqlTransaction`, breaking every
  subsequent tool call until a restart. Every mutator now funnels through one shared
  `_txn()` context manager (commit on success, rollback on any exception). The
  deep-dream apply loop is unaffected by design (each op is idempotent + re-runnable).
- **`world_cortex` / `lessons` supersession ignored HLC ordering.** Both stores
  superseded on value-difference alone, unlike the personal cortex; they now gate on
  the HLC (an out-of-order write with an older stamp can't clobber a newer value).
  Dormant under the shipped single-writer (every write gets a fresh monotonic tick),
  live under the future multi-writer path — parity with `cortex._should_supersede`.
- **`exact_duplicate_pairs` could auto-merge (no review) two `A<->B` concat
  artifacts**, and `merge_entity` over-counted / FK-crashed on a stale endpoint. The
  auto-merge path now excludes concat artifacts, and `merge_entity` returns a graceful
  no-op when either endpoint no longer exists instead of raising.

### Security
- **Stored-XSS via world-fact `source_url`.** A citation URL is agent/LLM-authored
  (often distilled from fetched web content), so a prompt-injected `javascript:` /
  `data:` scheme could execute when an operator clicked the "source" link in the
  Cortex Console. Now blocked at both ends: `service.world_write` rejects any non-
  `http(s)` scheme at write time (`{"action":"rejected"}`) so the payload never lands,
  and `views/world.js` allowlists `http(s)` at render time (bad URLs show as inert text).
- **`ops/restore_from_pt.py` unpickled snapshots with `weights_only=False`** (CWE-502),
  inconsistent with `storage/migrate.py`'s own guard on the same file format. Now
  `weights_only=True`, so restoring a stale/tampered `.pt` bank can't execute code.

## [0.6.0] — 2026-06-25 — graph foundation

### Added
- **Provenance-as-link (Phase 1)** — the dream now links each consolidated fact-slot
  to the dense episodes it came from (`memory_traces`, keyed on the stable slot);
  facts surface `source_entries`, and new `memory_get` / `memory_reinforce` tools
  dereference and strengthen them.
- **Cortex Console (web UI)** — an operator dashboard served by the daemon at
  `/ui/` (new `pseudolife_memory/web/`: a pure-ASGI `/api` REST layer 1:1 over
  `MemoryService` + a no-build vanilla SPA). Tabs: Observatory (health/stats,
  MIRAS band continuum, dream gauges), Cortex (fact review with provenance +
  version-history timeline + contested-fact resolve), World, Lessons, Episodes,
  Stream (search + ranking-trace debugger), Graph (force-directed visualiser +
  table view), and a Console config editor (28 knobs, live-vs-restart, atomic
  save with backup). `/api/*` is bearer-gated like `/mcp`; `/ui` + `/health`
  stay open. Offline-first (vendored OFL fonts, no CDN, no build step). A
  fixture-backed `pseudolife_memory.web.devserver` renders the UI without
  Postgres for development.
- **Graph insight layer** — `dream` now computes graph communities (persisted),
  god-nodes, surprising connections, and suggested questions. New read-only
  tools `memory_digest` and `memory_communities`; `memory_graph` nodes carry a
  `community` field.
- **`memory_recall`** — read-only multi-hop graph-traversal retrieval (MemCoT
  loop). Seeds from the query, walks the knowledge graph up to `hops`
  iterations (max 5), and returns entities, edges, paths, and supporting texts.
  Mechanical seed driver by default (deterministic, no LLM call); set
  `PSEUDOLIFE_RECALL_DRIVER=llm` to use the dream endpoint for seed resolution.
  `low_confidence: true` signals no seed matched — fall back to `memory_search`.
- **`memory_path` / `get_neighbors`** — two focused read-only graph MCP tools.
  `memory_path` returns the shortest path between two entities (targeted
  bidirectional search over the read-model, `max_hops` cutoff); `get_neighbors`
  returns an entity's 1-hop neighbours with an optional relation filter.

### Changed
- `memory_recall` mechanical seeder is now **query-first** — it seeds the
  question's subject(s) and uses search-hit matches only as a fallback,
  eliminating cross-talk noise on populous banks (bench: seed precision 1.0 vs
  0.262, zero answer-recall loss, ~4× fewer graph calls). `recall.driver=llm`
  unchanged.
- `memory_recall` now **hub-gates** graph expansion (graphify-derived) — high-degree
  hub entities are still returned as results but are not expanded *through*, with
  degree-aware frontier ordering and a per-hop budget. Cuts blast radius on
  hub-adjacent queries with no recall loss (bench: mean −118 tokens/q, −6.7
  entities/q, zero recall regression). Adaptive threshold
  (`recall.hub_percentile` / `recall.hub_floor`); disable via `recall.hub_gate=false`.
- The graph-insight digest now also refreshes on a `dream_run` with **no memory
  backlog**, so manual graph edits (cleanup, direct `graph_relate`) are reflected
  in `memory_digest` / `memory_communities` without waiting for a memory-bearing
  dream.
- Dream `exclude_sources` default now also skips `"status"` and `"log"` — store
  verbose status/log dumps under those sources to keep them searchable (in the
  bands) without the dream mining them into knowledge-graph clutter.
- Graph layer: single source of truth (Postgres `entities` hub + NetworkX
  read-model) behind a swappable `GraphStore` port. Apache AGE removed.
- **Dream extractor default → Gemma 4 E2B QAT (UD-Q4_K_XL).** Switched the baked
  sidecar model (`ops/Dockerfile.extractor` `MODEL_URL`) from PTQ Q4_K_M to the
  quantization-aware-trained UD-Q4_K_XL — smaller (2.44 vs ~2.9 GB) and faster on
  CPU at identical quality. Quant ladder (2026-06-24, `evals/`): facts gold 1.0 /
  stale 0.0, relations F1 0.75 (separate), lessons 5/6 — all equal to the old
  Q4_K_M, ~17–40% faster to consolidate. Lighter GGUF quants are dominated:
  UD-Q3_K_XL regresses relations (F1 0.62) and is bigger+slower; UD-Q2_K_XL
  craters lesson synthesis (3/6 — inverts polarity/outcome) and is the slowest.
  GGUF size floor is ~2.2 GB; genuine sub-1 GB needs the LiteRT 2-bit/mmap mobile
  build (a separate runtime, not wired here).

### Removed
- `memory_graph_query` (raw read-only Cypher) MCP tool and the `pseudolife-mcp
  age-sync` CLI mode. Multi-hop queries are served by `memory_graph`
  (neighborhood + derived/inverse edges + shortest path). The Postgres image no
  longer requires the Apache AGE extension. Run `ops/migrate_drop_age.py` once
  (back up first) to drop the AGE graph + extension from an existing bank — it
  supersedes the v0.4 `ops/migrate_v04.py` collision-fix migration.

## [0.5.1] — dream resilience

### Fixed
- **The dream stopped skipping memories on a failed extraction.** The extractor
  masked failures (timeout / network / malformed response) as an empty `[]`
  result, so `dream_run` advanced its cursor past those memories permanently —
  on the live CPU Gemma sidecar this skipped every dream during a too-short
  timeout window. `OpenAICompatExtractor` now **raises `ExtractorError`** on
  failure; `dream_run` **holds the cursor** (returns `extractor_failed`) so the
  memories are retried next sweep, and `synthesize_lessons` already leaves its
  signals pending. A genuine empty result (a successful call with no canonical
  facts) still writes nothing and advances, as before.
- **Extractor timeout was too short for CPU inference.** The default CPU sidecar
  (Gemma E2B Q4) generates at ~30 tok/s, so a full generation easily exceeded the
  old 20s timeout → `claims:0`. `extractor_timeout_seconds` default 20s → **240s**
  and `extractor_max_tokens` 1024 → **2048** (headroom for dense batches + slower
  end-user laptops). Both are now env-overridable —
  `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` / `PSEUDOLIFE_DREAM_MAX_TOKENS` (set in
  `ops/docker-compose.yml`) — alongside the existing `_BASE_URL` / `_MODEL` /
  `_API_KEY`.

### Added
- **`ops/wslconfig.example`** — a `.wslconfig` template that caps Docker
  Desktop's WSL2 VM (the stack needs ~2–4 GB resident; WSL2 otherwise balloons to
  ~50% of host RAM and caches without releasing). Copy to `%USERPROFILE%\.wslconfig`
  and `wsl --shutdown` to apply.

## [0.5.0] — cosine spine

### Changed
- **Removed the test-time-trained neural memory; bands are now plain cosine
  vector stores.** An A/B eval ([`docs/2026-06-21-neural-memory-investigation.md`])
  showed the MIRAS neural-retrieval blend *underperformed* pure cosine at every
  scale (the L2-self-reconstruction MLP over frozen embeddings is a regime
  mismatch for standalone retrieval — TITANS/HOPE are end-to-end sequence
  models). `band.retrieve` is now pure cosine; the store gate uses **novelty**
  surprise (`1 − max cos(x, existing)`). Deleted `memory/miras/objectives.py`,
  `update_rules.py`, `modules.py`, the HOPE chained-read, the neural-blend
  config (`neural_blend_weight` / `neural_warmup_updates`), `chain_residual`,
  and the dead `MemoryMLP` / `TitansMemoryBank`. The contrastive feature keeps
  suppression (drops the band-MLP step). `memory_stats` per-band fields no
  longer include `objective` / `update_rule` / `base_lr` / `memory_module`.
  `weights.pt` now persists only counters (no MLP weights); legacy state with
  weight blocks loads tolerantly (entries restored, weights ignored). The full
  neural machinery is archived on the `archive/neural-memory-titans` branch
  for a future sequence-model experiment. `MIRASBandSpec` keeps only capacity /
  cadence / promotion / eviction.

### Fixed
- **Durable-save failures no longer silent (F3).** A failed cortex/world/lessons
  snapshot used to be swallowed with a `logger.warning` while the tool call
  returned success — silent data loss in a memory system. The saves now raise
  `PersistenceError` (surfaced to the caller on tool paths; the background
  autosave/flush threads already catch, so they survive) and bump a
  health-visible `persist_errors` counter. The AGE *mirror* stays best-effort
  (rebuildable via age-sync) — only content persistence is hardened.
- **Version/schema drift (F5).** `pyproject` version `0.2.0 → 0.4.0`; `/health`
  now reports `schema` from `SCHEMA_META_VERSION` (was hardcoded `8`) plus the
  new `persist_errors`; the `mcp_server.py` header rewritten to describe the
  HTTP-daemon + auth architecture (was the obsolete v0.1 stdio/no-auth model);
  clarified that `cortex.SCHEMA_VERSION` is the file-mode snapshot format number,
  distinct from the Postgres `SCHEMA_META_VERSION`.

### Added
- **Writer-aware temporal memory (schema v11).** Every canonical write (cortex,
  world, lessons) now carries a temporal/provenance stamp: `tx_time` (write
  time), `valid_time` (event time — a lesson inherits its source signal's
  observation time, not the dream's write time: bitemporal), an
  `(hlc_phys, hlc_logical)` **Hybrid Logical Clock** that is the ordering
  authority for supersession (monotonic, immune to wall-clock steps — "newer
  wins" no longer depends on jittery wall time), and `writer_id`/`session_id`.
  The daemon reads an `X-PL-Writer` header per request (the shim forwards
  `PSEUDOLIFE_WRITER_ID`) and a per-connection `session_id`, so concurrent
  sessions/agents are distinguishable. Reads surface the stamp + a human `age`;
  new `memory_history(entity, attribute)` returns the per-slot version timeline.
  A dormant `write_mode=occ` seam (`version` column + `replace_facts_occ` stub)
  is laid for a future multi-process writer (Phase 2; raises `NotImplementedError`).
  **Collision fix:** the AGE graph is renamed off the DB role name
  (`pseudolife` → `pseudolife_graph`), every connection pins `search_path` to
  `public`, and a guarded backup-first migration (`ops/migrate_v04.py`, later
  superseded by `ops/migrate_drop_age.py` when AGE was removed) renames legacy
  graphs + drops shadow tables. `ops/retire_by_writer.py` supersedes a rogue
  writer's rows. Design + plan:
  `docs/specs/2026-06-21-writer-aware-temporal-memory-{design,plan}.md`.
- **Procedural / outcome memory — "lessons" (schema v10).** A fourth memory
  layer beside the personal and world cortex that learns from the agent's *own
  work*: what worked, what was a dead end, and what the user corrected. Keyed by
  `(task-type, aspect)`, each lesson carries an `outcome`
  (`success`/`failure`/`correction`) and a `polarity` (`+` do / `-` avoid) in its
  own `lessons` table (blast-radius isolated). Capture is cheap and in-session
  (`memory_outcome` logs a *signal*; user-tier `memory_fact_set` corrections are
  auto-tagged); synthesis is **single-writer** — the dream's LLM extractor distils
  accumulated `outcome_signals` into lessons (`extract_lessons`), with no
  deterministic floor (no extractor ⇒ no lessons, signals retained + age-pruned).
  Lessons are **graph-traversable**: a task-type becomes an `etype='task-type'`
  entity and each lesson adds a `prefers`/`avoids` edge (two new builtin
  relations) to the tool/source it concerns. New tools: `memory_outcome`,
  `memory_lesson_search` (embedding-on-query), `memory_lessons`,
  `memory_lesson_forget`. Config under `memory.lessons`. The auto-injected
  "lessons from past work" prompt block, an outcome-coloured graph view, and a
  Cypher-side AGE edge-property upgrade are deferred follow-ons. Design:
  `docs/specs/2026-06-20-procedural-outcome-memory-design.md`.
- **Dream consolidation (Tiers 0–2).** Pull recent associative memories, extract
  canonical `(entity, attribute, value)` facts, write them to the cortex, and
  advance a monotonic cursor so each memory is consolidated once (session-agnostic
  — no "session finished" event needed). A pluggable `DreamExtractor`
  (`memory/dream.py`) feeds one shared `service.dream_run` driver that owns cursor
  discipline. (Single-writer cortex — see Changed — makes the LLM dream the sole
  automatic writer; the regex is opt-in only.) Three tiers:
  - **Tier 0** — `memory_dream_run` (regex floor, headless, no LLM, on-box/free).
  - **Tier 1** — agent-driven via `memory_dream_pull` / `memory_dream_status` /
    `memory_dream_commit` and a copy-in `/dream` command
    (`examples/commands/dream.md`).
  - **Tier 2** — `OpenAICompatExtractor` + a daemon background sweep that fires on
    a configurable backlog+quiescence trigger, pointed at any OpenAI-compatible
    endpoint (Ollama, LM Studio, Haiku, OpenRouter, self-hosted) via
    `PSEUDOLIFE_DREAM_BASE_URL` / `_MODEL` / `_API_KEY`.

  Eligible sources and the trigger thresholds are configurable under
  `memory.dream`. Design: `docs/specs/2026-06-15-pluggable-dream-extractor-design.md`.
- **Abstention signal.** `memory_search` now returns `low_confidence` — `True`
  when the top score falls below `memory.search_confidence_floor` (default `0.0`
  = off), so the agent can choose to abstain rather than answer from a weak
  match. A cortex hit always overrides it (a canonical fact *is* the answer).
- **One-shot dream sweep.** `memory_dream_run(limit=…)` consolidates the whole
  eligible backlog in a single call (omit for the configured batch size).
- **Opt-in CPU LLM extractor sidecar.** A llama.cpp `compose --profile extractor`
  service (`ops/Dockerfile.extractor`, Gemma 4 E2B baked in) exposes an
  OpenAI-compatible endpoint for higher-quality dream consolidation, off by
  default. Plus `evals/` — an extractor-ladder benchmark that picks the minimum
  viable model (verdict: Gemma 4 E2B clears; see `evals/README.md`).
- **Tunable cortex abstention guard.** `memory.cortex.guard_min_score` (default
  `0.3` = prior hard-coded behaviour) sets the score at/above which a cortex fact
  counts as a confident answer and suppresses `low_confidence`. Raising it lets
  weak topically-adjacent facts stop blocking abstention. Calibrated as a pair
  with `search_confidence_floor`; the `evals/` guard sweep recommends
  `guard_min_score = 0.65` + `search_confidence_floor = 0.70` (doubles abstention
  recall at zero false-abstain). Behaviour-preserving at the default.
- **Dream slot resolver (off by default).** `memory.cortex.dream_slot_match_threshold`
  (default `0.0` = off) lets the dream pass map a paraphrased `(entity, attribute)`
  onto an existing slot (value-free `slot_embedding`, schema v8, additive) before
  writing, to catch small-model supersession forks. Calibration found no
  measurable benefit on the benchmark (stale-leak flat, a false-merge at `0.80`):
  the residual fragmentation traces to the deterministic regex auto-promote, not
  paraphrase — see `docs/specs/2026-06-19-single-writer-cortex-design.md` for the
  structural fix. Shipped off; enable only with the false-merge risk in mind.

### Changed
- **Single-writer cortex.** The LLM **dream** pass is now the sole *automatic*
  writer of canonical facts (plus explicit `memory_fact_set`). The deterministic
  regex auto-promote on `store` (`memory.cortex.auto_promote`) now defaults
  **off**, and the `dream_run` regex fallback is removed — an extractor that
  yields nothing writes nothing. Rationale: the regex mis-splits compound entity
  names (`"payments database host"` → `payments` / `database host`) and, running
  alongside the LLM dream, fragments one fact across sibling slots — the real
  cause of the residual stale-leak, not small-model paraphrase. New
  `NoOpExtractor` is the default when no extractor LLM is configured; the daemon
  logs a startup warning in that case. Behaviour change: a plain `store()` no
  longer populates the cortex. Design:
  `docs/specs/2026-06-19-single-writer-cortex-design.md`.
- **Extractor sidecar default-on.** `ops/docker-compose.yml` now starts the Gemma
  CPU extractor with the stack (dropped its `profiles` gate) and routes dream
  consolidation to it. Clearer names (anti-PEBKAC): compose project `pseudolife-mcp`
  (was the folder default `ops`); containers `pseudolife-mcp-{postgres,daemon,extractor}`;
  new-install volumes default to `pseudolife-mcp-{bank,state}`, env-overridable so
  existing installs keep `ops_pseudolife_*` via `ops/.env`.

### Added (cleanup tooling)
- **`ops/dedup_cortex.py`.** One-time, dry-run-first, reversible cleanup that
  collapses paraphrase sibling slots left by past auto-promotes
  (`MemoryService.cortex_dedup` / `CortexStore.dedup_siblings`): clusters current
  slots by value-free slot-embedding cosine, keeps the canonical (provenance tier,
  then recency), retires the rest (audit trail kept). Back up before `--apply`.

### Fixed
- **Reasoning models in `OpenAICompatExtractor`.** Thinking models (Qwen3, etc.)
  spent the entire token budget on a `<think>` trace and returned empty content,
  silently falling back to the regex floor. The extractor now sends
  `chat_template_kwargs:{enable_thinking:false}` and tolerantly parses the
  outermost JSON object (stripping ```json fences / leading prose). Non-thinking
  templates (e.g. Gemma) ignore the kwarg; extraction got both faster and more
  accurate across the board.

## [0.2.0] - 2026-06-14

The v0.2 line moves the bank off local files and onto a single-writer daemon
backed by Postgres, and adds a canonical-fact cortex and a typed knowledge
graph on top of the associative continuum.

### Added
- **Daemon + shim architecture.** A long-lived memory daemon owns the bank and
  serves MCP over HTTP on `127.0.0.1:8765`; every Claude Code session attaches
  through a torch-free stdio shim (`pseudolife-mcp`) that auto-starts the daemon
  if absent. Three CLI modes: `serve` (daemon), default (shim), `embedded`
  (the v0.1 in-process server — no daemon, no Postgres).
- **Postgres source of truth.** Postgres 16 + pgvector (bundled
  `ops/docker-compose.yml`, host port `5433`, external volume so `down -v` can't
  wipe the bank) is now durable storage; the in-memory MIRAS bands are a
  write-through cache hydrated at startup. Single writer = concurrent sessions
  can't clobber each other; entries are transactional.
- **Cortex (canonical facts).** Slot-keyed `(entity, attribute) -> current value`
  store with supersession-not-decay: `memory_fact_get` / `memory_fact_set` /
  `memory_fact_resolve` / `memory_fact_forget` / `memory_facts`. Slot-shaped
  facts in any `memory_store` auto-promote at a 0.5 confidence floor.
- **Provenance contenders.** Cortex facts carry a tier (`user > action > agent`);
  a weaker-tier write that conflicts with a stronger-tier fact is parked as a
  contender (surfaced in get/search) rather than silently overwriting, and
  settled with `memory_fact_resolve`.
- **Knowledge graph.** Typed entity graph (`memory_graph`, `memory_graph_relate`,
  `memory_graph_unrelate`, `memory_relation_define`, `memory_alias`) with a
  closed relation vocabulary, soft type hints, and transitive/inverse closure
  computed on read. Apache AGE mirror enables read-only openCypher via
  `memory_graph_query`.
- **World-knowledge cortex.** Durable cited/dated facts about external reality,
  persisted in Postgres and exposed through the daemon's MCP tools.
- **Tier C** (carried from late 0.1): episodes (`memory_episode_*`),
  multi-valued tags, and the consolidation workflow
  (`memory_consolidation_candidates` + `memory_consolidate`).
- **Optional retrieval boosters:** cross-encoder reranker (`rerank=True`) and a
  stdlib BM25 hybrid lexical pool (`bm25=True`), both off by default.
- **LAN sharing.** Run the daemon with `PSEUDOLIFE_MCP_HOST=0.0.0.0` and a
  `PSEUDOLIFE_MCP_TOKEN`; the daemon refuses to bind a non-loopback host without
  a token, and Postgres stays loopback-only.
- **Ops:** `ops/install-autostart.ps1` (Task Scheduler logon task),
  `ops/backup.ps1` (rotating `pg_dump`), `age-sync` to heal a drifted AGE mirror.

### Fixed
- **Alias-aware cortex lookup.** `memory_fact_get` / `cortex_lookup` now resolve
  entity aliases through the graph before reporting a miss, so a fact stored
  under a canonical name (e.g. `dev-box`) is reachable via any bound alias
  (e.g. `4090`) — honouring the documented contract that every fact lookup
  resolves aliases first.
- **Test isolation against the AGE schema.** PG-backed test fixtures now pin
  `search_path` to `public` before schema/truncate work and reap leaked
  backends. Previously, once a test created the AGE graph (whose schema name
  `pseudolife` equals the DB role), unqualified table names resolved to
  graph-schema shadow tables and `TRUNCATE` cleared the wrong ones — rows leaked
  across tests and `pytest tests/` showed order-dependent failures. The full
  suite (300 tests) is now green on repeat runs.

### Migration
- On first daemon run, a pre-v8 `cms_state.pt` in `PSEUDOLIFE_MCP_DATA_DIR` is
  auto-migrated into Postgres; the originals are renamed `*.pre-v8.bak` (never
  deleted). The MCP build is not save-compatible with the desktop Pseudolife app.

## [0.1.0] - Initial release

- In-process stdio MCP server exposing the neural memory layer: the MIRAS
  8-tier continuum (working → forever), ChromaDB reference bank, supersession,
  and contrastive learning. File-mode persistence (`cms_state.pt` + ChromaDB);
  no daemon, no Postgres. `memory_store` / `memory_search` / `memory_recent` /
  `memory_supersede` / `memory_delete` / `memory_stats` / `memory_save` plus the
  document RAG tools.

[0.8.1]: https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/tag/v0.8.1
[0.8.0]: https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/tag/v0.8.0
[0.7.0]: https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/tag/v0.7.0
[0.2.0]: https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/tag/v0.2.0
