# W2-E store forward review checklist

The paused store branch used an older daemon foundation and could not merge directly with the current startup and writer code. This branch merges master and adapts the existing store implementation to that foundation. It is a local handoff for the paused author; no PR or public push was made.

## Revisions and scope

- Original `rust-cf/w2e-store`: `544faad1829567e04fa9af759b37cdf65781bacf`, unchanged.
- Initial integrated master: `4b34c81ef053ae2966ac8605d2680c36a182e6cb`.
- Current master: `4a8a11a799679b711fc647ce1e0a383b95051c8b`, merged again at `91d389b8` after its HTTP admission work landed during validation.
- Forward merge: `2d8d90afe286db369a59a76abf5503a560270aad`.
- First adaptation tested on both platforms: `a830f94b07ed133234dc28d351f9c129297a7562`.
- Harness fixes: `08974997` authenticates readiness; `b91d21bd` reports missing old catalog fields without crashing; `c1591af5` fills the embedding cap in the cancellation fixture after its short request left the control uncaught.
- The original branch implements `memory_store`, labels, the store's episode resolution, slot extraction, and contradiction detection. Fact/set/world writes, contenders, supersession, HLC ticking, outcomes/lessons, standalone episode/title tools, autosave, and exit flush are not added by this forward. Their outstanding work remains with the paused author.

## Every conflicted file

Paths below are relative to `rust/`, unless stated otherwise.

| File | Resolution and reason |
|---|---|
| `.gitattributes` | Master: retain current text/fixture policies. |
| `Cargo.lock` | Start with master and let Cargo resolve the combined dependency declarations; no hand-merged lock entries. |
| `daemon/Cargo.toml` | Master plus the existing harness feature and UUID, casefold, and fancy-regex dependencies; add serde for the startup episode conversion. Retain master's platform dependencies and graph harness. |
| `daemon/README.md` | Master: current shared foundation instructions. |
| `daemon/divergences.md` | Master plus the original W2-E write-path section. Existing unimplemented behavior remains declared. |
| `daemon/harness/daemons.py` | Master: current isolation, launch, static fixture, and cleanup behavior. |
| `daemon/harness/gen_schema_sql.py` | Master: current schema generator. |
| `daemon/harness/pgdisposable.py` | Master: production-bank refusal and slice-specific disposable databases. |
| `daemon/harness/record_routes.py` | Master: current shared route recording. |
| `daemon/harness/run.py` | Master: current shared foundation harness, catalog normalizers, and scenario inventory. Adapt `w2e.py` to its explicit before-state API. |
| `daemon/spec.md` | Master plus the original W2-E contract section. |
| `daemon/src/bank.rs` | Master hydration, seating, dimension checks, and storage-row shape; add the original store's resident push/remove/row operations, sequence, and cue memo. |
| `daemon/src/config.rs` | Master, including `band_specs`; add the store gate, meta-filter, and auto-promotion config leaves. Capacity comes from the master's band specifications. |
| `daemon/src/embed.rs` | Master: current complete-model admission and embedding behavior. |
| `daemon/src/health.rs` | Master: current shared health behavior. |
| `daemon/src/http.rs` | Master dispatch and transport; restore only the feature-gated W2-E harness route and bank read guard. |
| `daemon/src/main.rs` | Master module list and lifecycle plus W2-E module declarations. |
| `daemon/src/mutants.rs` | Master: retain all current fixture/mutant hooks. W2-E uses the existing named-mutant API. |
| `daemon/src/search.rs` | Master: only default initialization for the newly added resident fields changes. |
| `daemon/src/service.rs` | Master initialization, complete startup candidate, publication, and clock-reseed retry; add mutable resident bank and episode write state. |
| `daemon/src/static_files.rs` | Master: current shared static serving. |
| `daemon/src/storage/mod.rs` | Master: current leased storage lifecycle. |
| `daemon/src/storage/schema.rs` | Master: current schema constructor. |
| `daemon/src/storage/tests.rs` | Master: current storage tests. |
| `daemon/src/txn.rs` | Master exactly: generic errors and recovery marker rules are authoritative. |

Nonconflicting master infrastructure, CI layout, producer census, startup loaders, and schema SQL were retained. Slice-owned modules and frozen golden values remain from the paused store branch.

The late merge brought current HTTP admission, its dependency/test/CI additions, and the schema-plan convention in CLAUDE.md. Only `daemon/Cargo.toml` conflicted: retain both the store's serde and master's http-body-util additions; retain master's futures dev dependency. The harness route now accepts master's counted `RequestBody` and reads it through master's `read_body`, preserving shared drain accounting and the feature-gated route. The type and helper are crate-visible for that bridge; their fields and behavior are unchanged.

## Writer seam and adaptations

All W2-E database access uses the leased writer; this slice owns no dedicated connection and has no OPEN-marker exception.

| Site | Access |
|---|---|
| `store::delete_evicted` | Existing `txn::run`: delete the victim and update `capacity_true_drops` together. |
| `store::cms_store` entry insert | Existing `txn::run`, with an explicit PostgreSQL error type for master's generic seam. |
| `episodes::WriteState::persist_episodes` | Existing `txn::run` per episode upsert. |
| `episodes::WriteState::reopen_client_session` | Existing `txn::run` for the reopen update. |
| `episodes::WriteState::persist_tombstones` | Existing `txn::set_meta`, which calls `txn::run`. |
| Former `episodes::hydrate` and `get_meta` | Removed duplicate raw writer reads; `episodes::from_startup` consumes master's complete candidate instead. Master's metadata and episode loaders already use `txn::with_client`. |

The resident bank keeps master's runtime dimension and integer access count. The store keeps its fresh double-precision surprise in `surprise_value`, its original serialized write mutex, and its cue memo. It fills the master's raw embedding field, while the normalized matrix is still extended or removed in place. No write transaction is nested inside another seam call.

## Validation, 2026-10-10

Commands ran from `rust/`, with `-j 2` on Windows and `-j 3` on the Linux fixture lane. Logs and JSON outputs use the `W2E-store-*` names in the private handoff evidence.

| Check | Revision | Result |
|---|---|---|
| Windows `cargo build -j 2 -p pseudolife-daemon --features harness,mutants` | `91d389b8` | Exit 0. |
| Windows `cargo fmt -p pseudolife-daemon --check` | `91d389b8` | Exit 0. |
| Windows `cargo clippy -j 2 -p pseudolife-daemon --all-targets --features harness,mutants` | `91d389b8` | Exit 0, no warnings. |
| Windows `cargo test -j 2 -p pseudolife-daemon --features harness,mutants` | `91d389b8` | Exit 0: 121 passed, 0 failed, 1 ignored. |
| Linux build, fmt, clippy, and crate tests with `harness,mutants` | `91d389b8` | Each exit 0; 126 passed, 0 failed, 1 ignored. No clippy warnings. |
| Linux `w2e.py live` | `91d389b8` | Exit 0: 4 scenarios, 69 steps, 0 differing steps. Full DB state compared after every operation. |
| Linux golden replay | `91d389b8` | Exit 1: 61 executed steps, 3 differing steps, all at scenario initialization; current catalog includes `column_types` and changed `owners_and_comments`/`sequence_bounds` shapes. The seeded scenario is skipped by the existing replay rule. No crash. |
| Linux store mutants, `--only store-defaults` | `91d389b8` | Exit 0: all 5 caught, none uncaught; tags 62 diffs, session episode 158, hint 6, surprise 31, cancellation 11. |
| Publication check over `origin/master..HEAD` | Checklist payload, checked again after final commit | Exit 0; no public push. |

Live numerical observations: response-surprise gap 0; maximum embedding difference `7.03999999990268e-7`; minimum cosine `0.9999999999927007`; stored-surprise gap `1.250000000008189e-6`. The original tolerances were retained.

Initial adaptation checks were not passes: the first Windows compile exited 101 (missing serde dependency, stale atomic test initialization, and generic transaction error inference); an initial fmt invocation from the repository root found no targets, a later fmt check exited 1 on a trailing blank line, and the first clippy run reported one item-order warning. Those were corrected before the final checks above. Two Linux harness starts were stopped using only their owned PID trees: the first readiness call omitted the fixture token, and the second fixture directory lacked the complete model layout required by master. Failed attempts and the later passing run are recorded separately.

The first all-scenario control run at `08974997` exited 1: four controls were caught, but the short cancellation request produced 0 differences. The final cap-sized cancellation fixture kept normal live parity and caught that control with 11 differences; all five controls were rerun against the defaults scenario. No Rust implementation changed for this fixture repair.

The optional PostgreSQL unit fixtures controlled by `PL_W1A_*` and `PL_PGS_*` were not supplied; their early-return bodies are not claimed as live database coverage. The store's own live harness supplied disposable PostgreSQL banks. The ignored full-docs corpus test is handled by the separate slots lane. No full Python suite or hosted CI ran.

## Decisions for Opus

1. Master's validated startup metadata is authoritative. The older episode loader's permissive conversions were replaced by the startup candidate conversion; review malformed metadata policy against the current startup contract.
2. The model fixture now needs a complete model directory, including tokenizer, model, module, and pooling metadata and the configured ONNX graph path. This was prepared privately from cached metadata and the existing export; master embedding code was unchanged.
3. Readiness previously omitted the fixture token: a watched probe returned false anonymously and true with the token. The harness now supplies it at every readiness call.
4. Master added the `column_types` catalog query. Old frozen store goldens omit that field; replay initially raised `KeyError`. The harness now reports that drift without suppressing it, including changed `owners_and_comments` and `sequence_bounds` shapes. All three replay differences are at initialization. Golden re-recording remains the paused author's decision.
5. Python oracle source did not change between the original store head and the integrated master for service, storage schema/postgres, labels, meta filter, slots, or contradiction. No tracked golden was re-recorded.
6. The first short cancellation request left its deliberate mutant uncaught. The fixture now fills the existing 512-token cap (160 repetitions) to keep embedding work active during the socket close; the watched failed control and passing repeat are retained privately. The frozen old replay does not cover this later-added operation.
7. This handoff does not open a PR or replace the paused author's independent review gate.

## Mutual branch merge

The scratch merge of store checklist head `0e1481c7` with slots checklist head `6f05ae78` exited 1: the two forwards do not merge cleanly. Its 11 conflicted files are:

- `rust/Cargo.lock`
- `rust/daemon/Cargo.toml`
- `rust/daemon/FORWARD-w2e.md`
- `rust/daemon/divergences.md`
- `rust/daemon/src/bank.rs`
- `rust/daemon/src/config.rs`
- `rust/daemon/src/http.rs`
- `rust/daemon/src/labels.rs`
- `rust/daemon/src/main.rs`
- `rust/daemon/src/meta_filter.rs`
- `rust/daemon/src/service.rs`

The scratch merge was aborted and both forward worktrees are clean. Neither original branch was modified. Both slot/contradiction implementations remain byte-identical; the conflicting checklists describe the two independent branches. The final follow-up commits change checklist text only.
