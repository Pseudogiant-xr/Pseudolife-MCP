# W2-E slots forward review checklist

The paused slot and contradiction branch used an older daemon foundation. This branch merges current master while retaining the original pure-function implementations and frozen Python cases. It is a local handoff for the paused author; no PR or public push was made.

## Revisions and scope

- Original `rust-cf/w2e-slots`: `c8a40fde67ec0319f5e998e42af1859c3a21a22b`, unchanged.
- Initial integrated master: `4b34c81ef053ae2966ac8605d2680c36a182e6cb`.
- Current master: `4a8a11a799679b711fc647ce1e0a383b95051c8b`, merged again at `8f250ae5` after its HTTP admission work landed during validation.
- Forward implementation: `ebcb00bed7e122dfadfabef1ecae1795a526acc0`.
- `slots.rs`, `contradiction.rs`, their recorder, and `w2e-slots.json` are unchanged from the original branch. Both implementation modules are also byte-identical to the store branch's versions.
- This branch implements extraction and possible-conflict detection. It does not add the later fact/set/world write APIs, HLC mutations, outcomes/lessons, episode/title tools, autosave, or exit flush.

## Every conflicted file

Paths below are relative to `rust/`.

| File | Resolution and reason |
|---|---|
| `.gitattributes` | Master: current text/fixture policies. |
| `Cargo.lock` | Start with master and let Cargo resolve the combined dependencies; no hand-merged lock entries. |
| `daemon/Cargo.toml` | Master plus the existing casefold and fancy-regex dependencies. Retain master's platform dependencies and graph harness. No harness-only write route exists on this branch. |
| `daemon/README.md` | Master: current shared foundation instructions. |
| `daemon/divergences.md` | Master: current foundation behavior; the original branch had no separate implemented store section to restore. |
| `daemon/harness/daemons.py` | Master: current fixture launch and isolation. |
| `daemon/harness/dbstate.py` | Master: complete current catalog, row, and before-state comparison. |
| `daemon/harness/gen_schema_sql.py` | Master: current schema generation. |
| `daemon/harness/pgdisposable.py` | Master: production-bank refusal and per-slice database names. |
| `daemon/harness/record_routes.py` | Master: current shared route recording. |
| `daemon/harness/run.py` | Master: current foundation scenarios and normalizers. |
| `daemon/spec.md` | Master plus the original W2-E contract section. It describes the wider planned write slice; that plan does not imply those tools exist here. |
| `daemon/src/auth.rs` | Master: current shared authentication. |
| `daemon/src/bank.rs` | Master: complete current hydration and seating. The pure functions do not require bank mutations. |
| `daemon/src/config.rs` | Master: current shared configuration. |
| `daemon/src/embed.rs` | Master: current embedding admission and loading. |
| `daemon/src/health.rs` | Master: current shared health. |
| `daemon/src/http.rs` | Master: current dispatch, transport, and static routes. |
| `daemon/src/main.rs` | Master plus the slot and contradiction module declarations. |
| `daemon/src/mutants.rs` | Master: current shared mutant hooks. |
| `daemon/src/search.rs` | Master: current shared search. |
| `daemon/src/service.rs` | Master: complete startup candidate and clock-reseed retry. |
| `daemon/src/static_files.rs` | Master: current shared static serving. |
| `daemon/src/storage/mod.rs` | Master: current leased storage lifecycle. |
| `daemon/src/storage/schema.rs` | Master: current schema constructor. |
| `daemon/src/storage/tests.rs` | Master: current storage tests. |

The older one-line `labels.rs` and `meta_filter.rs` stubs were removed from this forward; master has no such shared stubs, and this branch does not implement or call them. The original branch and worktree were not edited. All nonconflicting master infrastructure, startup loaders, CI layout, schema SQL, and producer census were retained.

The late merge brought current HTTP admission, its dependency/test/CI additions, and the schema-plan convention in CLAUDE.md. This merge was clean; all incoming files were retained from master. The pure slot/contradiction implementation and its corpus inputs are unchanged.

## Writer seam

The slice-owned slot and contradiction modules make no database calls and own no connection. There are no slice-owned write sites to adopt and no dedicated-client exception. Master `txn.rs`, storage, and startup remain authoritative and unchanged.

## Validation, 2026-10-10

Commands ran from `rust/`, with `-j 2` on Windows and `-j 3` on the Linux fixture lane. Private logs and outputs use `W2E-slots-*` names.

| Check | Revision | Result |
|---|---|---|
| Windows `cargo build -j 2 -p pseudolife-daemon` | `8f250ae5` | Exit 0. |
| Windows `cargo fmt -p pseudolife-daemon --check` | `8f250ae5` | Exit 0. |
| Windows `cargo clippy -j 2 -p pseudolife-daemon --all-targets` | `8f250ae5` | Exit 0, no warnings. |
| Windows `cargo test -j 2 -p pseudolife-daemon` | `8f250ae5` | Exit 0: 114 passed, 0 failed, 1 ignored. |
| Linux build, fmt, clippy, and crate tests | `8f250ae5` | Each exit 0: 119 passed, 0 failed, 1 ignored; no clippy warnings. |
| Current Python full-docs oracle and ignored Rust corpus test | `ebcb00be` | Both exit 0: 54,723 checked, 0 declared skips, 0 mismatches. Output is private; tracked goldens unchanged. |
| Scratch mutation controls | `ebcb00be` | Polarity and negation-threshold mutations each exited 101 with the named golden test mismatches; after restoring each original source, that test exited 0. Final checkout clean. |
| Publication check over `origin/master..HEAD` | Checklist payload, checked again after final commit | Exit 0; no public push. |

The full-docs and mutation proofs carry forward to `8f250ae5`: the slice functions, their Python oracle, their frozen cases, the corpus inputs, and `http::py_strip` are unchanged by the late HTTP merge.

The tests cover frozen slot extraction, developer slots, normalization, contradiction threshold boundaries, cue memo behavior, malformed/superseded rows, and Unicode classes/mappings. Optional PostgreSQL unit fixtures were not supplied; their early-return bodies are not claimed as database coverage. These slice-owned functions are pure; there is no standalone HTTP or database harness on the original slots branch. No full Python suite or hosted CI ran.

## Decisions for Opus

1. This is an independent forward of the slots branch, not a merge of the store branch into it.
2. Python slot, contradiction, cortex normalization, and label source files did not change between the original head and the integrated master. Frozen golden values were preserved. Any later golden re-recording remains the paused author's decision.
3. The existing Unicode-version exception remains as declared by the original tests; newly assigned code points are not claimed as exact Python 3.11 parity.
4. This handoff does not open a PR or replace the paused author's independent review gate.

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

## W2P preparation, 2026-10-12

- BASE: `aaa652c6f570d02d9b2985acf1b3da5e1c45e92d`.
- Local branch: `w2p/e-slots`; no publication.
- Conflicts: `src/main.rs`.

Both contradiction and dream module declarations are retained. Pure slot/contradiction sources and frozen cases are unchanged.

Existing Unicode-runtime exception remains; no DB access is added by this slice.

Validation and independent Codex high review are recorded in the private W2P report; initial failures and final reruns are listed separately. Windows evidence is cross compilation, not runtime. Frozen oracle goldens are not re-recorded.
